"""Run the Insight Agent desktop workbench with Python 3.11."""

import sys
import multiprocessing


def main():
    multiprocessing.freeze_support()
    if sys.version_info[:2] != (3, 11):
        print("本应用需要 Python 3.11 x64，请使用 start.bat 或 .venv311\\Scripts\\python.exe main.py")
        return 1
    try:
        from insight_agent.desktop import run
    except ModuleNotFoundError as error:
        print(f"缺少运行依赖 {error.name}。请执行：.venv311\\Scripts\\python.exe -m pip install -r requirements-lock.txt")
        return 1
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
