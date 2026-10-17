"""KernelSU LKM 镜像修补工具（PC 版）。

在电脑上为 Android 的 boot / init_boot / vendor_boot 镜像打入 KernelSU 的
LKM（Loadable Kernel Module）模式补丁，产出可直接 fastboot 刷入的镜像。

核心思路：不重复实现修补逻辑，而是驱动 KernelSU 官方发布的
`ksud-x86_64-pc-windows-gnu.exe`（与手机端 App 内使用的是同一份 patch 代码），
从而保证 PC 端产物与手机端“直接安装”的结果完全一致。
"""

__version__ = "1.0.0"
APP_NAME = "KernelSU LKM 镜像修补工具"
