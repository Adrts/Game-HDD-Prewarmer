# Game-HDD-Prewarmer

机械硬盘游戏的冷启动暖机加速器：按磁盘物理顺序把游戏目录里的小文件读入 Windows 页面缓存，再尽快启动游戏，减少随机小 IO 造成的暖机卡顿。

设计文档见 [docs/design.md](docs/design.md)。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe main.py
```

仅使用 Python 标准库（tkinter），无需安装第三方依赖。
