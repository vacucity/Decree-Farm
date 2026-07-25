"""
智能戒指蓝牙连接脚本
先扫描附近设备，然后连接到戒指并读取系统信息
"""

import asyncio
import ring_sound as sdk


# ============================================================
# 如果你已经知道戒指的 MAC 地址，填在这里（格式如 "F1:C1:8A:35:40:FB"）
# 不知道的话留空，脚本会先扫描
# ============================================================
KNOWN_MAC = "DA:2A:F8:9B:FE:44"


async def scan_and_find():
    """扫描附近蓝牙设备，让用户选择戒指"""
    print("正在扫描蓝牙设备（5秒）...\n")
    devices = await sdk.scan_rings(timeout_s=5.0)

    if not devices:
        print("[X] 没有扫描到任何蓝牙设备")
        print("   请确认：")
        print("   1. 电脑蓝牙已开启")
        print("   2. 戒指处于广播状态（未连接其他设备）")
        print("   3. 戒指电量充足")
        return None

    print(f"找到 {len(devices)} 个设备:\n")
    for i, d in enumerate(devices):
        name = d.name or "(无名称)"
        rssi = d.rssi or "?"
        print(f"  [{i}] {d.address}  |  信号: {rssi} dBm  |  名称: {name}")

    print(f"\n  [s] 手动输入 MAC 地址")

    choice = input("\n请选择设备编号 (或输入 s): ").strip()

    if choice.lower() == "s":
        return input("请输入 MAC 地址: ").strip().upper()

    try:
        idx = int(choice)
        if 0 <= idx < len(devices):
            return devices[idx].address.upper()
    except ValueError:
        pass

    print("无效选择")
    return None


async def connect_and_read_info(mac: str):
    """连接戒指并读取系统信息"""
    print(f"\n正在连接 {mac} ...")

    async with sdk.RingSoundClient(address=mac) as ring:
        print("[OK] 蓝牙已连接！\n")

        # 读取系统信息
        info = await sdk.get_system_info(ring)
        print("=" * 45)
        print("  戒指系统信息")
        print("=" * 45)
        print(f"  固件版本    : {info.firmware_version}")
        print(f"  设备型号    : {info.model}")
        print(f"  序列号(S/N) : {info.sn}")
        print(f"  CPU ID      : {info.cpuid}")
        print(f"  电量        : {info.battery_percent}%")
        charging = "充电中" if info.battery_charging else "未充电"
        print(f"  充电状态    : {charging}")
        print(f"  设备时间    : {info.system_time}")
        total = info.audio_storage_total or 0
        avail = info.audio_storage_available or 0
        print(f"  录音存储    : {avail} / {total} 字节可用")
        print("=" * 45)

        # 读取录音数量
        count = await sdk.get_audio_file_count(ring)
        print(f"\n  录音文件数量: {count}")

        print("\n[OK] 连接正常，戒指状态良好！")


async def main():
    # 如果填写了 MAC 地址就直接用
    mac = KNOWN_MAC.strip().upper() if KNOWN_MAC.strip() else None

    # 否则扫描
    if not mac:
        mac = await scan_and_find()

    if not mac:
        print("未获取到 MAC 地址，退出")
        return

    try:
        await connect_and_read_info(mac)
    except sdk.TransportError as e:
        print(f"\n[X] 连接失败 (蓝牙传输错误): {e}")
        print("   可能原因：设备不在范围内、已被其他设备连接、或 MAC 地址错误")
    except sdk.TimeoutError:
        print(f"\n[X] 连接超时，设备无响应")
    except sdk.DeviceError as e:
        print(f"\n[X] 设备返回错误: {e} (错误码 {e.error_code})")
    except Exception as e:
        print(f"\n[X] 未知错误: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=" * 45)
    print("  智能戒指 - 蓝牙连接工具")
    print("=" * 45)
    print()
    asyncio.run(main())
