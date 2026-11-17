from configparser import ConfigParser as _ConfigParser

from alembic.command import upgrade
from alembic.config import Config

from app.core.config import settings
from app.db import Engine, Base
from app.log import logger


def init_db():
    """
    初始化数据库
    """
    # 确保所有模型都已注册到 Base.metadata 中
    import app.db.models  # noqa: F401

    # 全量建表
    Base.metadata.create_all(bind=Engine) # noqa


def build_alembic_config() -> Config:
    """
    构造绑定当前运行目录与数据库 URL 的 Alembic 配置对象。
    """
    script_location = settings.ROOT_PATH / 'database'
    alembic_cfg = Config()
    alembic_cfg.file_config = _ConfigParser(interpolation=None)
    alembic_cfg.set_main_option('script_location', str(script_location))

    if settings.DB_TYPE.lower() == "postgresql":
        db_url = settings.DB_POSTGRESQL_URL()
    else:
        db_location = settings.CONFIG_PATH / 'user.db'
        db_url = f"sqlite:///{db_location}"

    alembic_cfg.set_main_option('sqlalchemy.url', db_url)
    return alembic_cfg


def update_db():
    """
    更新数据库（首次从上游 v3 切入时自动备份并执行无损结构与配置适配）
    """
    try:
        from app.db.v3_migration import migrate_v3_database_if_needed

        alembic_cfg = build_alembic_config()
        migrate_v3_database_if_needed(
            Engine,
            alembic_cfg,
            config_path=settings.CONFIG_PATH,
        )
        upgrade(alembic_cfg, 'head')
    except Exception as e:
        logger.error(f'数据库更新失败：{str(e)}', exc_info=True)
