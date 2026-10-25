"""PyInstaller 打包入口（见 .github/workflows/ci.yml 的 exe 任务）。双击 Lap.exe = python -m simtelemetry。"""
from simtelemetry.__main__ import main

if __name__ == "__main__":
    main()
