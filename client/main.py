"""CLI 入口：基于 Typer，子命令按模块组织，便于扩展。"""
import typer

from client.doctor import app as doctor_app

app = typer.Typer(
    name="eidolon",
    help="Eidolon 命令行工具",
    no_args_is_help=True,
)

app.add_typer(doctor_app, name="doctor", help="打印全部 Config Manager 配置。")


@app.command("pymock")
def pymock_cmd() -> None:
    """启动 GUI 客户端（pymock）。"""
    from client.pymock.gui_client import main as gui_main

    gui_main()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
