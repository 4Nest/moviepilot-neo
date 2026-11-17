from app.core.config import global_vars
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey


def _reload_synced_plugins(
    plugin_manager: PluginManager,
    synced_ids: list[str],
    has_resolved_deps: bool,
) -> list[str]:
    """按需热重载新同步或补齐依赖后待恢复的插件，避免全量 stop 导致运行中插件路由出现 404 空窗。"""
    target_ids: list[str] = list(dict.fromkeys(synced_ids or []))
    if has_resolved_deps:
        installed_ids = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
        for pid in installed_ids:
            if pid not in plugin_manager.running_plugins and pid not in target_ids:
                target_ids.append(pid)

    from app.api.endpoints import plugin as plugin_endpoint

    reloaded: list[str] = []
    for pid in target_ids:
        try:
            plugin_manager.reload_plugin(pid)
            plugin_endpoint.register_plugin_api(pid)
            reloaded.append(pid)
        except Exception as err:
            logger.error(f"增量初始化插件 {pid} 失败：{err}", exc_info=True)
    return reloaded


async def sync_plugins() -> bool:
    """
    初始化安装插件，并动态注册后台任务及API
    """
    try:
        loop = global_vars.loop
        plugin_manager = PluginManager()

        sync_result = await execute_task(loop, plugin_manager.sync, "插件同步到本地")
        resolved_dependencies = await execute_task(
            loop, plugin_manager.install_plugin_missing_dependencies, "缺失依赖项安装"
        )
        # 判断是否需要进行插件初始化
        if not sync_result and not resolved_dependencies:
            logger.debug("没有新的插件同步到本地或缺失依赖项需要安装")
            return False

        # 仅增量重载本次同步或待恢复的插件，避免全量 stop/start 中断其它运行态插件
        logger.info("正在增量初始化已同步或补齐依赖的插件")
        await execute_task(
            loop,
            lambda: _reload_synced_plugins(
                plugin_manager,
                list(sync_result or []),
                bool(resolved_dependencies),
            ),
            "增量插件重载",
        )
        logger.info("增量插件初始化完成")
        return True
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


async def execute_task(loop, task_func, task_name):
    """
    执行后台任务
    """
    try:
        result = await loop.run_in_executor(None, task_func)
        if isinstance(result, list) and result:
            logger.debug(f"{task_name} 已完成，共处理 {len(result)} 个项目")
        else:
            logger.debug(f"没有新的 {task_name} 需要处理")
        return result
    except Exception as e:
        logger.error(f"{task_name} 时发生错误：{e}", exc_info=True)
        return []


def register_plugin_api():
    """
    插件启动后注册插件API
    """
    from app.api.endpoints import plugin
    plugin.register_plugin_api()


def init_plugins():
    """
    初始化插件
    """
    PluginManager().start()
    register_plugin_api()


def stop_plugins():
    """
    停止插件
    """
    try:
        plugin_manager = PluginManager()
        plugin_manager.stop()
        plugin_manager.stop_monitor()
    except Exception as e:
        logger.error(f"停止插件时发生错误：{e}", exc_info=True)
