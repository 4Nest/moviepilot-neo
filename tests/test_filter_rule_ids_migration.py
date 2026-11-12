"""用隔离 SQLite 验证旧过滤规则 ID 与规则组引用的原子、保真迁移。"""

import copy
import importlib

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.modules.filter.RuleParser import RuleParser
from app.schemas.types import SystemConfigKey

_MIGRATION = "database.versions.a7383c1d9e20_filter_rule_ids"


@pytest.fixture
def migration_database(monkeypatch):
    """创建只含系统配置的临时数据库，并把新迁移绑定到调用方事务。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "systemconfig", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False, unique=True),
        sa.Column("value", sa.JSON()),
    )
    metadata.create_all(engine)
    migration = importlib.import_module(_MIGRATION)
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            yield migration, table, connection
    finally:
        engine.dispose()


def _save(connection, table, key, value) -> None:
    """向临时配置表写入一项配置。"""
    connection.execute(table.insert().values(key=key.value, value=value))


def _read(connection, table, key):
    """读取临时配置表中的一项 JSON 配置。"""
    return connection.execute(sa.select(table.c.value).where(table.c.key == key.value)).scalar_one()


def test_upgrade_preserves_rules_groups_and_other_configuration(migration_database) -> None:
    """迁移只改变非法 ID 及完整操作数引用，保留规则字段、组名和默认组选择。"""
    migration, table, connection = migration_database
    custom = [
        {"id": " Pre Candidate", "name": "优先候补组", "include": "QHstudIo|OurTV", "exclude": None},
        {"id": "M-Team", "name": "馒头", "include": "M-Team", "size_range": ">100", "extra": {"keep": True}},
        *[{"id": f"Rule{index}", "name": f"规则{index}"} for index in range(28)],
    ]
    groups = [
        {"name": f"组{index}", "media_type": "电视剧", "category": "国产剧",
         "rule_string": "1080P &  Pre Candidate > 4K" if index < 13 else "1080P",
         "extra": [index]}
        for index in range(27)
    ]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    _save(connection, table, SystemConfigKey.SearchFilterRuleGroups, ["组0", "组26"])
    migration.upgrade()
    updated = _read(connection, table, SystemConfigKey.CustomFilterRules)
    updated_groups = _read(connection, table, SystemConfigKey.UserFilterRuleGroups)
    expected = copy.deepcopy(custom)
    expected[0]["id"], expected[1]["id"] = "PreCandidate", "MTeam"
    assert updated == expected
    expected_groups = copy.deepcopy(groups)
    for group in expected_groups[:13]:
        group["rule_string"] = "1080P &  PreCandidate > 4K"
    assert updated_groups == expected_groups
    assert _read(connection, table, SystemConfigKey.SearchFilterRuleGroups) == ["组0", "组26"]
    migration.upgrade()
    migration.downgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == expected
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == expected_groups


def test_migration_avoids_builtin_custom_and_generated_id_collisions(migration_database) -> None:
    """清理后的 ID 冲突时追加稳定后缀，不覆盖内置、已有或另一条迁移结果。"""
    migration, table, connection = migration_database
    custom = [{"id": value, "name": value} for value in [
        "PreCandidate", "PreCandidate1", "Pre Candidate", "Pre-Candidate", "4-K", "中文", "123",
    ]]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [{
        "name": "优先级", "rule_string": "Pre Candidate > Pre-Candidate > 4-K > 中文 > 123",
    }])
    migration.upgrade()
    updated = _read(connection, table, SystemConfigKey.CustomFilterRules)
    assert [rule["id"] for rule in updated] == [
        "PreCandidate", "PreCandidate1", "PreCandidate2", "PreCandidate3", "4K1", "Rule", "Rule123",
    ]
    expression = _read(connection, table, SystemConfigKey.UserFilterRuleGroups)[0]["rule_string"]
    assert expression == "PreCandidate2 > PreCandidate3 > 4K1 > Rule > Rule123"
    for level in expression.split(">"):
        assert RuleParser().parse(level).as_list()


def test_reference_rewrite_only_replaces_complete_operands(migration_database) -> None:
    """同名片段不能误替换，括号和否定中的完整引用则同步迁移并保留排版。"""
    migration, table, connection = migration_database
    _save(connection, table, SystemConfigKey.CustomFilterRules, [
        {"id": "M-Team", "name": "馒头"}, {"id": "Prefix-M-Team", "name": "前缀组"},
    ])
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [
        {"name": "完整引用", "rule_string": "!(M-Team | Prefix-M-Team) & 1080P"},
        {"name": "相似片段", "rule_string": "Unknown-M-Team > M-Teams"},
        {"name": "空草稿", "rule_string": None},
    ])
    migration.upgrade()
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == [
        {"name": "完整引用", "rule_string": "!(MTeam | PrefixMTeam) & 1080P"},
        {"name": "相似片段", "rule_string": "Unknown-M-Team > M-Teams"},
        {"name": "空草稿", "rule_string": None},
    ]


@pytest.mark.parametrize("value", [None, []])
def test_empty_custom_rules_do_not_create_or_rewrite_configuration(migration_database, value) -> None:
    """新安装及没有自定义规则的配置保持原值，不创建额外规则组。"""
    migration, table, connection = migration_database
    _save(connection, table, SystemConfigKey.CustomFilterRules, value)
    migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == value
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 1


def test_ambiguous_whitespace_ids_fail_without_partial_changes(migration_database) -> None:
    """去除外围空白后无法唯一定位时明确失败，不能猜测后覆盖用户规则。"""
    migration, table, connection = migration_database
    custom = [{"id": " Rule", "name": "旧规则"}, {"id": "Rule", "name": "合法规则"}]
    groups = [{"name": "组", "rule_string": "Rule"}]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    with pytest.raises(RuntimeError, match="无法唯一定位"):
        migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == groups


def test_whitespace_id_cannot_take_over_builtin_references(migration_database) -> None:
    """带外围空白的旧 ID 与内置 ID 相同也必须拒绝猜测引用归属。"""
    migration, table, connection = migration_database
    custom = [{"id": " 4K", "name": "旧规则"}]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [{"name": "组", "rule_string": "4K"}])
    with pytest.raises(RuntimeError, match="无法唯一定位"):
        migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom


def test_subscription_and_workflow_group_references_stay_intact(monkeypatch) -> None:
    """订阅和工作流保存的是规则组名称；ID 迁移不应改写这些引用或动作配置。"""
    migration = importlib.import_module(_MIGRATION)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    config = sa.Table("systemconfig", metadata, sa.Column("key", sa.String(), primary_key=True),
                      sa.Column("value", sa.JSON()))
    subscribe = sa.Table("subscribe", metadata, sa.Column("id", sa.Integer(), primary_key=True),
                         sa.Column("filter_groups", sa.JSON()), sa.Column("version_rules", sa.JSON()))
    workflow = sa.Table("workflow", metadata, sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("actions", sa.JSON()))
    metadata.create_all(engine)
    subscribe_refs = {"filter_groups": ["组"], "version_rules": [{"filter_groups": ["组"]}]}
    workflow_actions = [{"type": "FilterTorrentsAction", "params": {"rule_groups": ["组"]}}]
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            _save(connection, config, SystemConfigKey.CustomFilterRules, [{"id": "M-Team", "name": "馒头"}])
            _save(connection, config, SystemConfigKey.UserFilterRuleGroups, [{"name": "组", "rule_string": "M-Team"}])
            connection.execute(subscribe.insert().values(**subscribe_refs))
            connection.execute(workflow.insert().values(actions=workflow_actions))
            migration.upgrade()
            assert connection.execute(sa.select(subscribe.c.filter_groups, subscribe.c.version_rules)).one() == (
                ["组"], [{"filter_groups": ["组"]}],
            )
            assert connection.execute(sa.select(workflow.c.actions)).scalar_one() == workflow_actions
            assert _read(connection, config, SystemConfigKey.UserFilterRuleGroups)[0]["rule_string"] == "MTeam"
    finally:
        engine.dispose()


def test_data_migration_belongs_to_single_startup_lineage() -> None:
    """新迁移应连接 NEO 当前 head，启动更新可自动发现且只有唯一 head。"""
    config = Config()
    config.set_main_option("script_location", "database")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["a7383c1d9e20"]
    assert script.get_revision("a7383c1d9e20").down_revision == "c9d0e1f2a3b4"


def test_failure_between_config_writes_rolls_back_both_rows(monkeypatch) -> None:
    """两个配置更新之间的数据库错误应回滚整个迁移，不留下半套引用。"""
    migration = importlib.import_module(_MIGRATION)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("systemconfig", metadata, sa.Column("key", sa.String(), primary_key=True),
                     sa.Column("value", sa.JSON()))
    custom = [{"id": "M-Team", "name": "馒头"}]
    groups = [{"name": "组", "rule_string": "M-Team"}]
    metadata.create_all(engine)
    with engine.begin() as connection:
        _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
        _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    writes = []

    def fail_second_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        """在第二个配置写入前注入故障。"""
        if statement.startswith("UPDATE"):
            writes.append(statement)
            if len(writes) == 2:
                raise RuntimeError("模拟第二次配置写入失败")

    sa.event.listen(engine, "before_cursor_execute", fail_second_write)
    try:
        with pytest.raises(RuntimeError, match="第二次配置写入失败"):
            with engine.begin() as connection:
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
                migration.upgrade()
        with engine.connect() as connection:
            assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom
            assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == groups
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_second_write)
        engine.dispose()
