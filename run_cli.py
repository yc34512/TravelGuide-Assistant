"""双击运行.bat 的交互入口：中文提示在这里做（Python 无编码问题），参数转交给 main.py。

- 双击（无参数）：首次自动弹出 API Key 配置向导，然后提示输入关键词跑完整流程
- 命令行带参数：原样转发，如  运行.bat 西湖 --no-llm
- 重新配置 Key：运行.bat setup   或   python run_cli.py setup
"""
import subprocess
import sys

from core.credentials import get_llm_config, interactive_setup


def main():
    args = sys.argv[1:]

    if args and args[0] == "setup":
        interactive_setup(force=True)
        print("配置完成，现在可以正常使用了。")
        return

    # 无参数（双击）路径：先确保 Key 已配置，再进入关键词提示
    if not args and get_llm_config() is None:
        if interactive_setup() is None:
            print("（本次将以“只采集不生成报告”模式运行）")
            args = ["--no-llm"]

    if not args:
        print("=" * 46)
        print("  旅游攻略助手 TravelGuide Assistant")
        print("  输入景点关键词，例如: 西湖攻略")
        print("  （直接回车退出）")
        print("=" * 46)
        kw = input("景点关键词: ").strip()
        if not kw:
            return
        args = [kw]

    print(f"使用解释器: {sys.executable}")
    raise SystemExit(subprocess.call([sys.executable, "main.py", *args]))


if __name__ == "__main__":
    main()
