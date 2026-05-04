"""doctor 子命令：打印当前所有 Config Manager 配置，支持表格与 JSON 两种展示。"""
from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app = typer.Typer(help="打印全部 Config Manager 配置（表格 + 可选 JSON）。")


def _table_from_dict(title: str, data: dict[str, Any], border_style: str = "dim") -> Table:
    """将字典渲染为 Rich Table。"""
    t = Table(title=title, border_style=border_style, show_header=True, header_style="bold")
    t.add_column("配置项", style="cyan", width=30)
    t.add_column("值", style="white")
    for k, v in data.items():
        if isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False, indent=2)
        elif isinstance(v, bool):
            v = "true" if v else "false"
        elif v is None:
            v = "null"
        t.add_row(k, str(v))
    return t


def _mask_secret(value: Any, mask_char: str = "*") -> str:
    """脱敏处理：None 返回 "null"，非空字符串显示前后各2字符中间用 mask_char 填充。"""
    if value is None:
        return "null"
    s = str(value)
    if len(s) <= 4:
        return mask_char * len(s)
    return s[:2] + mask_char * (len(s) - 4) + s[-2:]


def _render_config_manager(console: Console) -> None:
    """打印 ConfigManager 元信息。"""
    # 直接从子模块导入，绕过 eidolon.shared.__init__ 的 logging 依赖
    from eidolon.shared.config.config_manager import ConfigManager

    try:
        cm = ConfigManager()
    except (ValueError, FileNotFoundError) as e:
        console.print(Panel(f"[red]ConfigManager 初始化失败: {e}[/red]", title="ConfigManager", border_style="red"))
        return

    data = {
        "config_dir": str(cm.config_dir),
        "default_yaml": str(cm.default_path),
        "env": cm._env or "null",
        "loaded": cm._loaded,
        "keys_count": len(cm._config),
    }
    console.print(_table_from_dict("ConfigManager", data, "blue"))
    console.print()


def _render_hub_config(console: Console) -> None:
    """打印 HubConfig。"""
    from eidolon.hub.config import HubConfig

    try:
        cfg = HubConfig.resolve()
        rows: dict[str, Any] = {}
        rows["verify_code"] = _mask_secret(cfg.verify_code)
        rows["transport.websocket.enabled"] = cfg.transport_cfg.websocket.enabled
        rows["transport.websocket.host"] = cfg.transport_cfg.websocket.host
        rows["transport.websocket.port"] = cfg.transport_cfg.websocket.port
        rows["transport.mqtt.enabled"] = cfg.transport_cfg.mqtt.enabled
        rows["transport.mqtt.broker"] = cfg.transport_cfg.mqtt.broker
        rows["transport.livekit.enabled"] = cfg.transport_cfg.livekit.enabled
        rows["transport.livekit.url"] = cfg.transport_cfg.livekit.url
        rows["session.heartbeat_interval"] = cfg.session_cfg.heartbeat_interval
        rows["session.session_timeout"] = cfg.session_cfg.session_timeout
        console.print(_table_from_dict("HubConfig", rows, "cyan"))
    except ModuleNotFoundError:
        console.print(Panel("[yellow]HubConfig 模块不存在，跳过。[/yellow]", title="HubConfig", border_style="yellow"))
    except Exception as e:
        console.print(Panel(f"[red]HubConfig 加载失败: {e}[/red]", title="HubConfig", border_style="red"))
    console.print()


def _render_pipeline_config(console: Console) -> None:
    """打印 PipelineConfig。"""
    try:
        from eidolon.pipeline.config import PipelineConfig
    except ModuleNotFoundError:
        console.print(Panel("[yellow]eidolon.pipeline.config 模块不存在，跳过。[/yellow]", title="PipelineConfig", border_style="yellow"))
        console.print()
        return

    try:
        cfg = PipelineConfig.resolve()
        rows: dict[str, Any] = {}
        rows["mode"] = cfg.mode
        rows["audio.sample_rate"] = cfg.audio.sample_rate
        rows["audio.channels"] = cfg.audio.channels
        rows["audio.codec"] = cfg.audio.codec
        rows["audio.frame_ms"] = cfg.audio.frame_ms
        rows["vad.enabled"] = cfg.vad.enabled
        rows["vad.model"] = cfg.vad.model
        rows["vad.threshold"] = cfg.vad.threshold
        rows["vad.min_speech_duration_ms"] = cfg.vad.min_speech_duration_ms
        rows["vad.min_silence_duration_ms"] = cfg.vad.min_silence_duration_ms
        rows["stt.enabled"] = cfg.stt.enabled
        rows["stt.model"] = cfg.stt.model
        rows["stt.model_name"] = cfg.stt.model_name
        rows["stt.endpoint_duration_ms"] = cfg.stt.endpoint_duration_ms
        rows["stt.vad_enabled"] = cfg.stt.vad_enabled
        rows["turn_detection.enabled"] = cfg.turn_detection.enabled
        rows["turn_detection.model"] = cfg.turn_detection.model
        rows["turn_detection.eot_threshold"] = cfg.turn_detection.eot_threshold
        rows["turn_detection.stability_window_ms"] = cfg.turn_detection.stability_window_ms
        rows["agent.enabled"] = cfg.agent.enabled
        rows["agent.mode"] = cfg.agent.mode
        rows["agent.socket_path"] = cfg.agent.socket_path
        rows["agent.endpoint"] = cfg.agent.endpoint
        rows["agent.timeout"] = cfg.agent.timeout
        rows["tts.enabled"] = cfg.tts.enabled
        rows["tts.model"] = cfg.tts.model
        rows["tts.voice_id"] = cfg.tts.voice_id
        console.print(_table_from_dict("PipelineConfig", rows, "cyan"))
    except ModuleNotFoundError:
        console.print(Panel("[yellow]PipelineConfig 模块不存在，跳过。[/yellow]", title="PipelineConfig", border_style="yellow"))
    except Exception as e:
        console.print(Panel(f"[red]PipelineConfig 加载失败: {e}[/red]", title="PipelineConfig", border_style="red"))
    console.print()


def _render_agent_config(console: Console) -> None:
    """打印 AgentConfig。"""
    from eidolon.agent.config import AgentConfig

    try:
        cfg = AgentConfig.resolve()
        rows: dict[str, Any] = {}
        rows["mode"] = cfg.mode
        rows["llm.provider"] = cfg.llm.provider or "null"
        rows["llm.model"] = cfg.llm.model or "null"
        rows["llm.base_url"] = cfg.llm.base_url or "null"
        rows["llm.api_key"] = _mask_secret(cfg.llm.api_key)
        rows["llm.temperature"] = cfg.llm.temperature
        rows["llm.max_tokens"] = cfg.llm.max_tokens
        rows["llm.context_window"] = cfg.llm.context_window
        rows["llm.timeout"] = cfg.llm.timeout
        rows["memory.enabled"] = cfg.memory.enabled if cfg.memory else "null"
        if cfg.memory and cfg.memory.graph:
            rows["memory.graph.enabled"] = cfg.memory.graph.enabled
            rows["memory.graph.uri"] = cfg.memory.graph.uri or "null"
        if cfg.memory and cfg.memory.vector:
            rows["memory.vector.enabled"] = cfg.memory.vector.enabled
            rows["memory.vector.url"] = cfg.memory.vector.url or "null"
            rows["memory.vector.collection"] = cfg.memory.vector.collection or "null"
        rows["context.session_max_turns"] = cfg.context.session_max_turns if cfg.context else "null"
        rows["context.daily_ttl_hours"] = cfg.context.daily_ttl_hours if cfg.context else "null"
        rows["proactive.enabled"] = cfg.proactive.enabled if cfg.proactive else "null"
        rows["proactive.check_interval_seconds"] = cfg.proactive.check_interval_seconds if cfg.proactive else "null"
        console.print(_table_from_dict("AgentConfig", rows, "cyan"))
    except ModuleNotFoundError:
        console.print(Panel("[yellow]AgentConfig 模块不存在，跳过。[/yellow]", title="AgentConfig", border_style="yellow"))
    except Exception as e:
        console.print(Panel(f"[red]AgentConfig 加载失败: {e}[/red]", title="AgentConfig", border_style="red"))
    console.print()


def _render_storage_config(console: Console) -> None:
    """打印 StorageSettings。"""
    from eidolon.shared.storage.sqlite.config import load_storage_config

    try:
        cfg = load_storage_config()
        rows: dict[str, Any] = {}
        rows["db_path"] = str(cfg.resolve_db_path())
        rows["db_url"] = cfg.resolve_db_url()
        rows["echo_sql"] = cfg.echo_sql
        console.print(_table_from_dict("StorageSettings", rows, "cyan"))
    except ModuleNotFoundError:
        console.print(Panel("[yellow]StorageSettings 模块不存在，跳过。[/yellow]", title="StorageSettings", border_style="yellow"))
    except Exception as e:
        console.print(Panel(f"[red]StorageSettings 加载失败: {e}[/red]", title="StorageSettings", border_style="red"))
    console.print()


def _render_structured(console: Console) -> None:
    """以分组表格输出所有配置。"""
    _render_config_manager(console)
    _render_hub_config(console)
    _render_pipeline_config(console)
    _render_agent_config(console)
    _render_storage_config(console)


def _render_json(console: Console) -> None:
    """以 JSON 格式输出所有配置。"""
    from eidolon.shared.config.config_manager import ConfigManager

    result: dict[str, Any] = {}

    try:
        cm = ConfigManager()
        result["ConfigManager"] = {
            "config_dir": str(cm.config_dir),
            "default_yaml": str(cm.default_path),
            "env": cm._env,
            "loaded": cm._loaded,
            "config_keys": list(cm._config.keys()),
        }
    except Exception as e:
        result["ConfigManager"] = {"error": str(e)}

    try:
        from eidolon.hub.config import HubConfig

        hub = HubConfig.resolve()
        hub_data = hub.model_dump(mode="json")
        if hub_data.get("settings", {}).get("device_verify_code"):
            hub_data["settings"]["device_verify_code"] = "[MASKED]"
        result["HubConfig"] = hub_data
    except ModuleNotFoundError:
        result["HubConfig"] = {"skipped": "module not found"}
    except Exception as e:
        result["HubConfig"] = {"error": str(e)}

    try:
        from eidolon.pipeline.config import PipelineConfig
        pipeline = PipelineConfig.resolve()
        result["PipelineConfig"] = pipeline.model_dump(mode="json")
    except ModuleNotFoundError:
        result["PipelineConfig"] = {"skipped": "module not found"}
    except Exception as e:
        result["PipelineConfig"] = {"error": str(e)}

    try:
        from eidolon.agent.config import AgentConfig
        agent = AgentConfig.resolve()
        agent_data = agent.model_dump(mode="json")
        settings = agent_data.get("settings") or {}
        llm = settings.get("llm") or {}
        if llm.get("api_key"):
            llm["api_key"] = "[MASKED]"
        result["AgentConfig"] = agent_data
    except ModuleNotFoundError:
        result["AgentConfig"] = {"skipped": "module not found"}
    except Exception as e:
        result["AgentConfig"] = {"error": str(e)}

    try:
        from eidolon.shared.storage.sqlite.config import load_storage_config
        storage = load_storage_config()
        result["StorageSettings"] = {
            "db_path": str(storage.resolve_db_path()),
            "db_url": storage.resolve_db_url(),
            "echo_sql": storage.echo_sql,
        }
    except ModuleNotFoundError:
        result["StorageSettings"] = {"skipped": "module not found"}
    except Exception as e:
        result["StorageSettings"] = {"error": str(e)}

    body = json.dumps(result, ensure_ascii=False, indent=2)
    console.print(Panel(body, title="All Config Managers (JSON)", border_style="blue"))


@app.callback(invoke_without_command=True)
def doctor_cmd(
    ctx: typer.Context,
    json_only: bool = typer.Option(False, "--json", "-j", help="仅输出完整 JSON，不输出表格。"),
) -> None:
    """打印全部 Config Manager 配置。默认：分组表格；加 --json 时仅输出 JSON。"""
    if ctx.invoked_subcommand is not None:
        return

    console = Console()
    if json_only:
        _render_json(console)
    else:
        console.print(Panel("[bold cyan]Eidolon Config Doctor[/bold cyan]", title="Eidolon", border_style="bold cyan", padding=(1, 2)))
        console.print()
        _render_structured(console)
        console.print()
        hint = Text("使用 ", style="dim") + Text("python -m client doctor --json", style="bold") + Text(" 可查看完整 JSON。", style="dim")
        console.print(hint)
