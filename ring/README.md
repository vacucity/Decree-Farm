# Ring Sound SDK 技术说明

本文档说明 Ring Sound Python SDK 的文件组成、内部通信层次、协议边界，以及录音从设备采集到生成 WAV 的完整数据格式。公开函数的参数和调用示例请优先查阅 [ring_sound_use.md](ring_sound_use.md)，原始命令字段请查阅 [protocol.md](protocol.md)。

本文档核对基线：

- Python SDK：`ring_sound.py`，版本 `0.4.1`
- 通信协议：语音戒指 v4
- 设备端固件：`V2.000.0001.0015`

设备端源码、SDK 和协议文档可能独立更新。发生冲突时，当前设备端实际实现是判断设备行为的首要依据；本目录的 `protocol.md` 只维护 Python SDK 当前公开功能使用的协议。

## 1. 目录文件作用

| 文件                  | 面向对象        | 作用                                                                                  |
|---------------------|-------------|-------------------------------------------------------------------------------------|
| `ring_sound.py`     | SDK 使用者和维护者 | 单文件 Python SDK。包含 BLE 扫描与连接、协议收发、系统信息、日志、校时、录音下载、IMU/动作事件、Speex 解码和命令行入口。           |
| `ring_sound_use.md` | SDK 使用者     | SDK 调用手册。说明环境安装、公开数据类型、公开函数、参数、返回值、异常和业务示例。                                         |
| `protocol.md`       | 协议联调人员      | Python SDK 当前公开功能使用的 v4 通信协议字段表。                                                    |
| `README.md`         | SDK 使用者和维护者 | 本文档。解释文件关系、SDK 内部分层、字节序、协议包重组、能力边界及音频格式。                                            |
| `demo.apk`          | 需要查看蓝牙通信    | 已编译的 Android/uni-app 安装包，用于通过蓝牙连接录音设备并提取、播放原始录音、查看imu数据及手势识别情况；不是 Python SDK 的运行依赖。 |
| `戒指打印模型/`           | 需要打印外壳      | 已解压的戒指机械结构 STEP 模型，包含外圈或外壳及按键；不是 Python SDK 的运行依赖或公开 API。                           |

`ring_sound.py` 可以直接放入其他 Python 项目并通过以下方式导入：

```python
import ring_sound as sdk
```

`sdk` 只是模块别名，不会改变模块内容。`ring_sound.py` 中的 `__all__` 是公开 API 清单；没有出现在 `__all__` 中的名称应视为内部实现，即使 Python 仍允许通过模块属性访问。

### 1.1 Android APK

从 APK 内置 `manifest.json` 可以确认以下信息：

| 项目 | 值 |
| --- | --- |
| 应用名 | `bluetest` |
| uni-app ID | `__UNI__308E163` |
| Android 包名 | `uni.app.UNI308E163` |
| 版本 | `5.0.1`，version code `100` |
| 页面标题 | `语音单个功能测试` |
| 应用说明 | 蓝牙连接并提取、播放录音设备原始录音数据 |
| 主要权限 | Bluetooth、BLE Scan/Connect、位置权限 |

该 APK 是独立的 Android 应用交付物：

- Python 项目不需要解压、导入或安装该文件。
- 使用 `ring_sound.py` 时不依赖 APK 是否存在。
- APK 可用于 Android 侧功能验证，但其内部实现和 Python SDK 不是同一套运行代码。
- APK 界面中出现的功能不属于 `ring_sound.py.__all__`，不能据此推断 Python SDK 的公开接口。
- 安装前应确认文件来源。当前文件 SHA-256 为 `3952650E9B339746D30C7D23B9F65790BAEB69A69460649564FEEB78B21D10FF`。

### 1.2 戒指打印模型

`戒指打印模型/` 是独立的机械结构资源目录，共有 `7`、`9`、`10`、`11` 四个分组、8 个 STEP 文件，总大小为 `538595` 字节：

| 分组 | 外圈或外壳 | 按键 |
| --- | --- | --- |
| `7` | `戒指打印模型/7/7外圈.STEP` | `戒指打印模型/7/7按键.STEP` |
| `9` | `戒指打印模型/9/9外圈.STEP` | `戒指打印模型/9/9按键.STEP` |
| `10` | `戒指打印模型/10/10外壳.STEP` | `戒指打印模型/10/10按键.STEP` |
| `11` | `戒指打印模型/11/11外圈.STEP` | `戒指打印模型/11/11按键.STEP` |

这些文件可直接使用支持 STEP 的 CAD 软件查看或转换。`7/9/10/11` 在本目录中只表示模型分组编号，本文档不将其解释为某种标准戒指尺码。进行打印、加工或装配前，应在 CAD 软件中确认模型单位、比例、公差、材料、打印方向和装配间隙。

机械模型与 Python SDK 相互独立：使用 `ring_sound.py` 不需要加载模型文件，模型中包含的结构也不代表 SDK 提供了对应的软件接口。

## 2. SDK 内部分层

```text
业务脚本
  |
  |  get_system_info() / download_audio_file() / wait_sensor_data() ...
  v
高层功能函数
  |
  v
RingSoundClient
  |  请求与响应匹配、命令队列、主动事件分发
  v
PacketStream + BinaryReader/BinaryWriter
  |  BLE 分片重组、协议头、CRC、网络字节序字段
  v
NusClient
  |  bleak 扫描、连接、写特征值、订阅通知
  v
戒指 Nordic UART Service
```

### 2.1 BLE 传输层

当前 SDK 使用 Nordic UART Service（NUS）：

| 用途 | UUID | 数据方向 |
| --- | --- | --- |
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` | 服务标识 |
| TX Characteristic | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` | 戒指通知 Python |
| RX Characteristic | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` | Python 写入戒指 |

SDK 使用 MAC 地址筛选设备，不依赖广播设备名。`NusClient` 通过 `bleak` 工作：

1. 按 MAC 地址扫描目标设备，默认最长扫描 25 秒。
2. 扫描未返回目标对象时，尝试使用地址直接连接。
3. 订阅 TX 特征值通知。
4. 向 RX 特征值写入时固定按 20 字节分片，最后一片按实际剩余长度发送。

固定 20 字节是 SDK 的发送策略，不会关闭 Windows、Bleak 或系统蓝牙栈的 MTU 协商。设备发送给 Python 的通知也不受该发送策略限制，仍可能使用其他分片长度；`PacketStream` 会持续缓存收到的任意长度分片，并按 v4 协议包头重新组包。

BLE 一次通知不等于一个完整业务协议包。一个协议包可能被拆成多次通知，多包也可能连续到达，因此不能直接把单次 BLE 回调数据交给包体解析函数。

### 2.2 协议包重组

`PacketStream.feed()` 持续缓存 BLE 字节，并按照 magic 和 `body_length` 提取完整协议包：

```text
BLE chunk 1 ----\
BLE chunk 2 -----+--> PacketStream --> Packet(command, body, version, body_crc)
BLE chunk 3 ----/
```

如果流中出现 magic 之前的无效字节，`PacketStream` 会跳过这些字节并尝试重新同步。包体超过 SDK 的 `MAX_BODY_LENGTH`（当前为 5120 字节）、CRC 不正确或包头不合法时，会抛出 `ProtocolError`。`RingSoundClient` 等待命令时会直接传递该异常；等待期间 BLE 断开则抛出 `TransportError`。

### 2.3 请求响应与主动上报

`RingSoundClient` 为每个 command 维护一个异步队列：

- `request(request_command, response_command)`：清理对应响应命令的旧数据，发送请求，然后等待响应。
- `wait_for_command(command)`：只等待指定命令，适用于 `0x0605`、`0x0701` 等主动上报。
- `add_packet_handler(command, handler)`：为长期主动事件注册回调；自动校时使用该机制。

设备主动上报的数据会先进入命令队列，即使调用方当时没有正在执行 `wait_for_command()`。但应用仍应及时消费持续数据，避免积压旧包。

## 3. v4 协议包格式

当前协议头固定为 11 字节：

| 偏移 | 字段 | 长度 | 字节序 | 说明 |
| ---: | --- | ---: | --- | --- |
| 0 | `magic_number` | 1 | 无 | 固定为 `0x3F` |
| 1 | `version` | 2 | 大端 | SDK 发送版本 4，并接受不大于 4 的版本 |
| 3 | `command` | 2 | 大端 | 例如 `0x0101`、`0x0505` |
| 5 | `body_length` | 4 | 大端 | 包体字节数，不包含 11 字节包头 |
| 9 | `body_crc` | 2 | 大端 | 包体 CRC16；空包体时为 0 |
| 11 | `body` | 可变 | 按命令定义 | 命令包体 |

除字符串和原始二进制数据外，当前协议包体中的 `u16`、`i16` 和 `u32` 字段均按网络字节序，也就是大端序解析。SDK 的 `BinaryReader` 和 `BinaryWriter` 负责这一转换。

CRC 只覆盖 `body`，不覆盖协议头。SDK 使用初始值 `0xFFFF` 的 `crc16_compute()`，算法与设备端 `crc16_compute()` 一致。

> 录音 `.bin` 内部的 Speex 帧长度是一个明确的例外：该长度来自设备内存写入，使用 2 字节小端序。它不是 v4 包体的普通整数协议字段。

## 4. 当前公开 SDK 能力

| 能力 | 协议/设备状态 | Python SDK 状态 | 建议 |
| --- | --- | --- | --- |
| 系统信息 `0x0101/0x0102` | 当前可用 | 提供 `get_system_info()` | 使用高层函数 |
| 日志 `0x0301` 至 `0x0304` | 当前可用 | 提供存储信息和分块读取函数 | 使用高层函数 |
| 校时 `0x0401/0x0402` | 当前可用，设备主动请求 | 提供自动和手动响应函数 | 通常使用 `enable_time_sync()` |
| 录音 `0x0501` 至 `0x0509` | 当前可用，保存成功后还会主动上报连续 `0x0505` | 提供普通/quick 下载和自动录音接收 | 指定文件优先 `download_audio_file(..., quick=True)`；即时接收使用 `receive_auto_audio_file()` |
| 清空录音 `0x050B/0x050C` | 当前可用 | 提供 `clear_audio_files()` | 破坏性操作，谨慎调用 |
| 实时 IMU `0x0601` 至 `0x0605` | 当前可用，`0x0605` 为批量采样 | 提供 `SensorDataBatch` | 按批量结构解析 |
| 动作事件 `0x0701` 至 `0x0704` | 当前可用 | 均有等待和解析函数 | 按设备模式和事件语义调用 |
| Ogg、PCM、WAV 处理 | 设备不负责 | SDK 本地能力 | 可离线使用，不需要连接戒指 |

## 5. 设备端录音编码流程

### 5.1 从 PDM 到单通道 PCM

设备的 DMIC/PDM 驱动输出以下 PCM 数据：

| 参数 | 当前值 |
| --- | --- |
| 采样率 | 16000 Hz |
| 采样位深 | 16 bit signed PCM |
| 输入声道 | 2，左右声道交错 |
| 单次采集块 | 100 ms |
| 每声道每块采样数 | 1600 |
| 输入块大小 | `1600 * 2声道 * 2字节 = 6400` 字节 |

固件处理每个输入块时，取交错数据中的左声道，并执行当前实现中的增益处理 `left * 2`，得到：

- 16000 Hz
- 16 bit
- 单通道
- 每块 1600 个采样点，即 3200 字节 PCM

因此，SDK 默认的 `PcmConfig(sample_rate=16000, channels=1, bit_depth=16)` 与当前设备编码输入一致。

### 5.2 Speex 编码参数

设备使用 Speex Wideband 编码器：

| 参数 | 当前值 | 含义 |
| --- | ---: | --- |
| Speex mode | Wideband / `speex_wb_mode` | 适用于 16 kHz |
| `FRAME_SIZE` | 320 个采样点 | 每帧 20 ms |
| `ENCODE_QUALITY` | 3 | 固定质量等级 |
| `COMPLEXITY` | 3 | 编码复杂度 |
| `HIGHPASS` | 1 | 开启 Speex 高通 |
| `MAX_PACKET` | 540 字节 | 设备和 SDK 的保护上限 |

一个 100 ms PCM 块包含 1600 个单通道采样点，因此通常被编码成 5 个 Speex 帧：

```text
1600 samples / 320 samples per frame = 5 frames
```

在当前 Wideband、质量 3 配置下，SDK 兼容表将单个 Speex payload 估算为 20 字节。实际解析必须读取设备写入的长度前缀，不能把 20 字节硬编码成当前文件格式。

### 5.3 设备保存的 Speex 数据格式

设备保存的录音数据不是 WAV，也不是完整的 Ogg Speex 文件，而是连续的长度前缀 Speex 帧：

```text
[speex_length: uint16 little-endian]
[speex_payload: speex_length bytes]
[speex_length: uint16 little-endian]
[speex_payload: speex_length bytes]
...
```

例如 payload 长度为 20 字节时，前两个字节为：

```text
14 00
```

其中 `0x0014 = 20`，按照小端序存放。SDK 使用 `struct.unpack_from("<H", ...)` 读取该字段。

按“每帧 payload 20 字节”估算：

```text
每 100 ms: 5 * (2字节长度 + 20字节payload) = 110字节
每秒:      约 1100字节
每分钟:    约 66000字节
```

这些数值用于理解数据规模，文件解析和进度计算仍应以实际 `speex_length` 和设备返回的 `data_size` 为准。

### 5.4 Flash 文件头与下载数据

设备 Flash 中每条录音前还有一个内部 `audio_data_head_t`，用于保存：

```text
magic | file_size | timestamp | next_addr | head_crc
```

该头部只用于设备文件管理。`comp_audio_save_read_file_data()` 会从文件数据区读取，因此 `0x0505` 中的 `data` 和 SDK 保存的 `.bin`：

- 不包含 Flash 文件头；
- 从第一个 2 字节 Speex 长度字段开始；
- `AudioFileInfo.data_size` 表示编码数据区大小。

## 6. 录音传输链路

### 6.1 普通链路

普通链路由调用方计算下一帧偏移：

```text
0x0503 开始提取
  -> 0x0504 文件信息
  -> 0x0506 请求 offset=0
  -> 0x0505 返回数据
  -> 0x0506 请求 offset=上一帧 offset + frame_size
  -> 0x0505 返回数据
  -> ...
  -> 0x0507 结束提取
  -> 0x0508 结束响应
```

对应 SDK 调用是 `download_audio_file(..., quick=False)`。`get_audio_file_info()`、`read_audio_frame()` 和 `end_audio_extract()` 是这条链路的分步接口，主要用于协议调试。

### 6.2 quick 链路

quick 链路由设备连续推进偏移并主动发送数据：

```text
0x0509 开始快速提取
  -> 0x0504 文件信息
  -> 0x0505 offset=0
  -> 0x0505 offset=下一位置
  -> ...
  -> 最后一包 0x0505, is_end=1
```

对应 SDK 调用是默认的 `download_audio_file(..., quick=True)`。SDK 会：

1. 根据 `frame_offset` 合并数据。
2. 忽略已经收到的重叠字节。
3. 发现 offset 缺口或等待超时时发送 `0x0506` 请求缺失位置。
4. 最多连续补传 3 次；仍不连续时抛出 `ProtocolError`。
5. 最终按照 `AudioFileInfo.data_size` 截断结果。

设备当前单个 `0x0505` 数据区最多读取 4096 字节。加上 `error_code`、索引、偏移、大小和结束标记后，包体仍在 SDK 的 5120 字节限制内。

### 6.3 录音保存后自动发送

当前固件在录音保存成功后固定启动已保存文件的自动发送：

- 主动发送连续 `0x0505`；
- 自动发送路径不发送 `0x0504` 文件信息；
- `0x0505.file_index` 标识刚保存的录音，最后一帧通过 `is_end=1` 标识。

该行为与应用主动调用 `download_audio_file()` 是两条不同入口。`download_audio_file()` 会发送 `0x0509` 或 `0x0503`，重新提取指定索引；`receive_auto_audio_file()` 正常从录音保存后自动到达的 `0x0505` 开始接收，不需要预先发送提取命令：

```python
file_index, raw_audio = await sdk.receive_auto_audio_file(
    ring,
    timeout_s=60.0,
)
bundle = sdk.save_audio_bundle(
    file_index=file_index,
    data=raw_audio,
    output_dir="audio",
)
```

SDK 以第一帧的 `file_index` 确定文件，按 `frame_offset` 拼接并校验重复或重叠数据。正常连续接收时不发送额外命令；出现偏移缺口、等待超时或损坏帧时，SDK 先通过 `0x0503 -> 0x0504` 获取并校验准确文件长度，再发送 `0x0506` 请求当前期望偏移，最多补传 3 次。恢复后的结果严格裁剪为 `data_size`，无法恢复时不会返回不完整数据。

自动上报本身不会预先发送 `0x0504`，所以函数返回值仍为 `(file_index, raw_audio)`，不包含 `AudioFileInfo`。只有需要恢复时 SDK 才在内部查询元数据，用于校验索引和最终长度。调用期间必须保持 BLE 已连接，而且不能让 `receive_auto_audio_file()` 与 `download_audio_file()` 或 `read_audio_frame()` 并发消费同一连接的 `0x0505` 队列。如果连接建立过晚而错过主动上报，应稍后查询录音数量，再使用 `download_audio_file()` 下载指定索引。

## 7. SDK 的 Speex 解码流程

### 7.1 总体流程

当前设备下载数据的标准解码路径是：

```text
下载的 .bin
  -> 解析 2 字节小端长度前缀
  -> 得到独立 Speex payload
  -> SDK 构建 Ogg Speex 容器
  -> ffmpeg 解码为 s16le PCM
  -> SDK添加 WAV 文件头
  -> 保存 .wav
```

`save_audio_bundle()` 会先保存原始 `.bin`，再执行格式识别和解码。缺少 ffmpeg 或解码失败时，`.bin` 仍然保留，函数随后抛出 `SpeexDecoderUnavailable` 或 `AudioDecodeError`。

### 7.2 输入格式识别顺序

`decode_speex_to_pcm()` 和 `build_playable_audio()` 按以下顺序处理：

1. 已经是 WAV：`build_playable_audio()` 直接返回，不重复解码。
2. 已经是 Ogg Speex：直接交给 ffmpeg。
3. 长度前缀 Speex：调用 `parse_packetized_speex_stream()`。
4. 无长度前缀的裸 Speex：按 `bits_size` 或质量映射尝试固定长度切分。
5. 都不符合：抛出 `AudioDecodeError`。

SDK 返回的 `source_type` 含义：

| 值 | 含义 |
| --- | --- |
| `wav` | 输入已经是 WAV |
| `ogg-speex` | 输入已经包含 Ogg 和 Speex 头 |
| `packet-speex` | 当前设备使用的长度前缀 Speex 流 |
| `raw-speex` | 没有长度前缀，SDK 按固定帧长度推测切分的兼容格式 |

### 7.3 `quality`、`bits_size` 和分块兼容

当前设备文件有明确的长度前缀，因此正常解码时不依赖 `quality` 或 `bits_size`。

- `quality`：仅在裸 Speex 固定长度回退中选择估算帧长，默认值为 3。
- `bits_size`：调用方明确指定裸 Speex 每帧字节数时覆盖质量映射。
- `allow_framed_blocks`：允许解析 1026 字节外层分块输入，是 SDK 当前公开的本地兼容能力。

SDK 的质量映射如下：

| quality | 估算字节数 | quality | 估算字节数 |
| ---: | ---: | ---: | ---: |
| 1 | 10 | 6 | 28 |
| 2 | 15 | 7 | 38 |
| 3 | 20 | 8 | 38 |
| 4 | 20 | 9 | 46 |
| 5 | 28 | 10 | 46 |

`parse_packetized_speex_stream()` 自身默认允许外层分块；`decode_speex_to_pcm()`、`build_playable_audio()`、`decode_audio_to_wav()` 和 `save_audio_bundle()` 默认不启用该兼容选项。处理 1026 字节分块输入时应显式传入 `allow_framed_blocks=True`。

### 7.4 SDK 构建的 Ogg Speex

ffmpeg 不能直接识别设备的“长度前缀 Speex 流”，所以 SDK 先通过 `build_ogg_speex()` 构建临时 Ogg 容器。默认参数包括：

| Ogg/Speex 字段 | SDK 默认值 |
| --- | --- |
| Speex 标识 | `Speex   ` |
| 版本标识 | `speex-1.2.1` |
| 采样率 | 16000 Hz |
| mode | 1，Wideband |
| mode bitstream version | 4 |
| 声道 | 1 |
| frame size | 320 |
| VBR 标志 | 0 |
| frames per packet | 1 |
| vendor comment | `ring-sound-python` |

SDK 为 Speex 头、comment 和每个音频 packet 创建 Ogg page，音频 granule position 每包增加 320。生成的 Ogg 数据主要作为 ffmpeg 的中间输入，`save_audio_bundle()` 默认不会单独保存它。

ffmpeg 输出参数为：

```text
format: s16le
channels: 1
sample rate: 16000
```

随后 `build_wav_from_pcm()` 添加标准 44 字节 PCM WAV 头。`normalize_decoded_speex_pcm()` 用于处理部分解码器可能为单个 packet 产生多帧 PCM 的兼容情况，目标长度是每个 Speex packet 对应一个 320 采样点 PCM 帧。

## 8. 音频格式与扩展名

| 名称 | 是否压缩 | 是否自带播放参数 | 当前用途 |
| --- | --- | --- | --- |
| 设备 `.bin` | 是 | 否 | SDK 保存的原始长度前缀 Speex 流，便于留档和重新解码 |
| 裸 Speex payload | 是 | 否 | 单个编码帧，不能仅凭扩展名直接确定全部参数 |
| Ogg Speex `.spx`/`.ogg` | 是 | 是 | SDK 为 ffmpeg 构建的标准容器，可被支持 Speex 的工具识别 |
| PCM `s16le` | 否 | 否 | ffmpeg 解码后的原始采样，需要另外知道采样率、声道和位深 |
| WAV `.wav` | 否 | 是 | PCM 加 RIFF/WAV 头，适合直接播放和进一步分析 |

不能只把设备 `.bin` 改名为 `.wav` 或 `.spx`。扩展名不会增加缺失的容器头，也不会移除每帧的 2 字节长度字段。

## 9. 其他数据链路要点

### 9.1 批量 IMU `0x0605`

设备当前只有录音模式和手势模式。手势模式就是本地 IMU 采集已开启的状态，不存在独立的“IMU 数据采集模式”。要让 IMU 数据通过 BLE 到达 SDK，还必须成功发送 `0x0601`：

```text
connect
-> 用户将设备切换到手势模式（本地 IMU 采集已开启）
-> start_sensor_report（发送 0x0601，并收到成功的 0x0602）
-> wait_sensor_data 循环接收 0x0605
-> stop_sensor_report（发送 0x0603）
```

`start_sensor_report()` 只打开 `0x0605` BLE 上报开关，不会启动本地 IMU；在录音模式调用会收到设备忙碌。`wait_sensor_data()` 只等待数据，不会自动发送 `0x0601`。`stop_sensor_report()` 只关闭 BLE 上报，不会停止手势模式内部继续使用的 IMU 采集。

当前 `0x0605` 是一批已经由固件解析好的六轴采样，不是传感器 FIFO 原始字节：

```text
error_code:    u16 big-endian
sequence_start:u32 big-endian
frame_count:   u16 big-endian
sample_size:   u16 big-endian，当前必须为16
samples:       frame_count * sample_size
```

每个 16 字节 sample 为：

```text
timestamp_ms:u32
accel_x:i16 accel_y:i16 accel_z:i16
gyro_x:i16  gyro_y:i16  gyro_z:i16
```

SDK 会严格验证 `sample_size == 16` 和剩余包体长度，并返回 `SensorDataBatch`。

`0x0702` HMM 手势识别直接使用手势模式下的设备内部 IMU 数据，本身不要求调用 `start_sensor_report()`；只有应用还需要同时接收实时 `0x0605` 时才需要开启 BLE 上报。

### 9.2 动作事件

| 命令 | 当前语义 | 包体 |
| --- | --- | --- |
| `0x0701` | 普通双击事件；手势模式长按会话期间抑制 | `timestamp_ms:u32` |
| `0x0702` | HMM 手势识别结果 | `timestamp_ms:u32 + gesture_id:u8` |
| `0x0703` | 独立按键双击 | `timestamp_ms:u32` |
| `0x0704` | 按键单击确认；设备尝试切换录音/手势模式 | `timestamp_ms:u32` |

`gesture_id` 当前定义为 `0 idle`、`1 rotate_back`、`2 rotate_front`、`3 wave`。使用 `sensor_gesture_name()` 可以安全处理未知值。

### 9.3 设备模式与按键状态机

设备维护录音模式和手势模式两个工作模式，启动后默认处于录音模式。当前协议没有查询模式或主动设置模式的命令，SDK 只能接收按键事件，并结合已知初始状态和后续设备行为判断模式。

| 用户操作 | 设备处理 | 协议事件 | 模式影响 |
| --- | --- | --- | --- |
| 单击 | 按下时间小于 `200 ms`；经过 `50 ms` 消抖，释放后等待 `500 ms` 双击判定窗口 | `0x0704` | 窗口结束后尝试在录音和手势模式之间切换 |
| 双击 | 第二次有效短按发生在第一次释放后的 `500 ms` 内 | `0x0703` | 不切换模式，也不再产生对应的 `0x0704` |
| 长按 | 按住超过 `300 ms` 后，根据当前模式开始对应操作 | 无独立长按事件 | 录音模式开始录音；手势模式开始 HMM 手势会话 |
| 长按松开 | 结束当前录音或手势会话 | 录音数据或 `0x0702` 由对应流程产生 | 模式本身不变化 |

单击从录音模式切换到手势模式时，设备解除语音模式对 IMU 的阻止并启动本地 IMU 采集；从手势模式切回录音模式时，设备关闭本地手势采集和 `0x0605` 实时上报，并重新阻止 IMU 采集。

模式切换是一次“尝试”，不是 `0x0704` 的确认结果。设备忙碌、IMU 启动失败或仍处于长按收尾阶段时，切换可能未完成，但单击回调仍会发送 `0x0704`。该事件只有时间戳，不携带切换前后的模式。

为防止长按松开被误识别为模式切换，固件在长按松开后的 `250 ms` 内忽略短按触发的切换请求。底层仍可能发送对应的 `0x0704`，因此开发者不能把收到 `0x0704` 等同于切换成功。

### 9.4 当前有效 LED 状态

设备使用 RGB LED 表示正在进行的操作或错误，不使用灯光持续指示空闲状态下的录音模式或手势模式。模式切换成功本身没有专用灯效。

| 触发条件 | 当前灯效 | 结束条件 | 开发者应如何理解 |
| --- | --- | --- | --- |
| 录音开始并进行中 | 绿灯常亮 | 录音正常完成后熄灭 | 表示当前正在录音，不表示设备长期处于录音模式 |
| 手势长按会话进行中 | 红灯常亮 | 松开并结束手势会话后熄灭 | 表示当前正在采集手势，不表示设备长期处于手势模式 |
| 模式切换、录音或手势启动失败 | 红灯闪烁 3 次，约 `100 ms` 亮、`100 ms` 灭 | 动画结束后熄灭 | 表示本次操作失败；需要结合协议异常和设备日志定位原因 |
| 正在充电 | 红灯约 `500 ms` 亮、`500 ms` 灭 | 充满、断开充电或状态异常 | 表示充电状态检测为 charging |
| 充电完成 | 绿灯常亮 | 断开充电或状态异常 | 仅当充电动画已启动并从 charging 转为 full 时显示 |

录音或手势启动时，如果设备读取到的电量低于 `20%`，固件会拒绝本次操作并执行红灯错误闪烁。由于“手势进行中”和“错误”都使用红色，开发者应通过常亮与三次闪烁区分两者。

固件源码中仍保留广播、连接、断开和文件传输等 LED 回调定义，但当前没有完整的有效调用链；本表不将这些定义描述为当前设备行为。LED 动画按优先级和触发顺序执行，短暂错误提示可能临时覆盖正在显示的业务灯效。

## 10. 常见格式问题

### `.bin` 下载成功但无法播放

这是预期现象。`.bin` 是长度前缀 Speex 流，不是播放器通用容器。使用 `save_audio_bundle()` 生成 WAV，并确认 ffmpeg 已安装。

### ffmpeg 报输入格式错误

先确认数据是否从第一个 Speex 长度字段开始，是否混入 `0x0505` 的协议字段，或者是否在 BLE 分片尚未重组完成时就保存。传给解码函数的应是按 `frame_offset` 合并后的 `AudioDataFrame.data`。

### 解码时长与录音时长不一致

检查是否存在丢帧、重叠帧、错误的 `frame_offset`，以及是否误用了裸 Speex 的固定长度回退。当前设备数据应优先解析 2 字节小端长度前缀。

### 收到一组没有 `0x0504` 的 `0x0505`

这通常是当前固件在录音保存后自动发送的链路，不是 `download_audio_file()` 发起的 quick 下载。使用 `receive_auto_audio_file()` 接收；该函数通过首帧 `file_index` 区分文件，按偏移拼接。连续数据读取到 `is_end=True` 后返回；需要恢复时会先取得准确文件长度并校验完整性。

## 11. 维护时的同步检查

设备固件或协议更新后，建议依次核对：

1. NUS UUID、协议版本、11 字节包头和 CRC 是否变化。
2. 设备实际注册的命令，而不只看 `protocol.md` 的命令列表。
3. `0x0505` 最大数据大小、录音保存后的自动发送行为和录音结束标识。
4. Speex mode、采样率、帧采样数、质量、声道处理和长度前缀字节序。
5. `0x0605` 的 `sample_size` 和批量结构。
6. `0x0701` 至 `0x0704` 的触发条件和事件语义。
7. `ring_sound.py.__all__` 与 `ring_sound_use.md` 的公开 API 是否一致。
8. 按键消抖、短按、长按、双击窗口和长按收尾抑制时间是否变化。
9. 录音/手势模式的进入条件、IMU 状态和 `0x0605` 上报开关是否变化。
10. 当前实际触发的 LED 颜色、闪烁周期、结束条件和优先级是否变化。
11. `戒指打印模型/` 的分组、STEP 文件名称及机械版本是否变化。

协议字段发生变化时，应同时核对设备端、`ring_sound.py` 和 `protocol.md`，并同步更新 `ring_sound_use.md` 与 `README.md`；仅更新协议文档不能保证现有 SDK 已经支持新行为。
