# Rust BT/PT 识别改进验证记录

日期：2026-10-15。本地扩展 `0.2.9+neo.3` 基于上游 `v0.2.9`，分支 `fix/bt-pt-recognition`，兼容 Python 3.11+，未新增运行时依赖。

## 修复行为

原报告中的发布标题现在识别为 `令和妖神斑小姐 / Reiwa No Dara-San`、E13、1080p，字幕标签不再覆盖片名。

本轮万条测试继续修复了片名内 `【招架】/【Parry】` 被拆开、前置字幕标签污染名称、纯英文合集混入 `10-bit`、字幕标签后的 `BDRip` 覆盖英文别名、全角电影标签失效、括号内分辨率遗漏及文件名边缘空白问题。规则按发布结构处理，没有硬编码作品或站点名称。

前两轮修复继续保留：多语言别名、数字片名、合集范围及 Unicode 分隔符、修订集号、`S00E02 ... (S02E00)` 首组主编号、`S2026E100` 年度季号、PV/NCOP/NCED/Menu 编号、外挂字幕语言尾缀、目录继承、识别词和显式 ID 优先级。

`ANIME_EPISODE_REVISION=1` 保持兼容，`BT_PT_RECOGNITION_REVISION=3` 标识万条验证后的能力。后端通过 `anime_names_resolved` 复用 Rust 已确认名称，避免重复 Python 解析。旧扩展和关闭 Rust 的路径仍保留 Python 兜底。

## 万条语料与正确性

最终语料包含 **10,000 个不同输入、58,949 个字段断言**：**1,447 条真实输入 + 8,553 条语义保持格式变体**。标题入口 9,629 条，路径入口 371 条。不能将其称为一万条真实采集或一万部作品。

| 去重后首个来源 | 真实输入 | 含派生后的测试条数 |
| --- | ---: | ---: |
| mikan | 51 | 799 |
| dmhy | 70 | 1,178 |
| anibt | 24 | 369 |
| nyaa | 1,254 | 7,219 |
| audiences | 47 | 417 |
| user-report | 1 | 18 |

本轮新增标注 582 条新采集标题/文件，并为 68 条已有 Mikan/DMHY/AniBT 文本补做独立标注；同文跨站、同标题副标题按输入去重。audiences 使用既有采集文本的 47 个独立输入。Nyaa 真实文件占比较高，同作品不同集、字幕版本和特典分别计数。

期望来自独立阅读标题、副标题和文件列表，不使用被测解析器输出生成。变体涵盖字幕标签位置、括号样式、分辨率、发布组、修订号、文件后缀、父目录和空白。固定万条生成器按名称与变换类型轮流采样，不依据通过率筛选。来源审查后补充的标注增加 59 个不同真实输入；旧新版均重新使用同一份最终语料比较。

| 集合 | 上一版 neo.2 | 本版 neo.3 |
| --- | ---: | ---: |
| 真实输入 | 1,425 / 1,447 | 1,447 / 1,447 |
| 格式变体 | 8,318 / 8,553 | 8,553 / 8,553 |
| Rust 万条合计 | 9,743 / 10,000 | 10,000 / 10,000 |
| 后端万条合计 | 9,743 / 10,000 | 10,000 / 10,000 |

通过表示所有已标注字段匹配。这是定向回归集，不代表全站准确率，也不验证外部媒体库匹配；没有推断缺失的绝对集数映射或混合资源包内容。257 条改善输入共享规则根因，不能等同于 257 个不同 bug。

后端 `tests/cases/bt_pt_10k.jsonl.gz` 与 Rust `tests/metainfo/recognition_10k.jsonl.gz` 完全一致，清单及 SHA-256 校验防止复制遗漏。`tests/test_bt_pt_recognition.py` 分别调用真实 `MetaInfo`、`MetaInfoPath`，禁止 `_build_meta_info` 回退，后端独立测试不依赖相邻仓库。

最终未压缩 JSONL SHA-256：`54998a82a28c768b1664d14504e8149682ce173c624d8c1072bcf04279ee8aff`。

## 性能

本机 macOS arm64、Python 3.12、release wheel；同一万条输入，每组 3 轮、共 5 组，依次测不同模式，取每轮中位数。后端采用临时测试配置和 `custom_words=['#']`，计时前已预热。

| 路径 | 每一万条中位耗时 |
| --- | ---: |
| 单独 Rust neo.2 | 577.81 ms |
| 单独 Rust neo.3 | 575.93 ms |
| 后端 + neo.2 | 700.71 ms |
| 后端 + neo.3 | 700.95 ms |
| 后端纯 Python | 2966.54 ms |

新版完整调用性能与上一版基本持平，约为 Python 路径的 **4.23 倍**速度。不包含网络、数据库、TMDB 匹配或下载耗时。原始分组、每组计时和基线失败详情见 Rust 仓库 `tests/metainfo/recognition_10k_report.json`；前两轮历史报告仍保存在 `recognition_report.json`。

从后端根目录复测：

```sh
venv/bin/python -m pytest tests/test_bt_pt_recognition.py -q
venv/bin/python -m scripts.benchmark_recognition_10k --output /tmp/recognition-rust.json
venv/bin/python -m scripts.benchmark_recognition_10k --python-only --output /tmp/recognition-python.json
```

脚本先调用 `prepare_backend()` 隔离真实配置和数据库，再运行完整入口。可将旧 wheel 安装到独立目录，用 `PYTHONPATH` 运行同一脚本，避免更换当前安装。

## 检查与安装状态

- 后端全量：**12,934 passed、1 skipped**；全局网络守卫禁止真实出站。
- 扩展 Python 全量：**12,049 passed、1 skipped、90 subtests passed**。
- Rust：`cargo test --lib` **28 passed**；Clippy、rustfmt 和 diff 空白检查通过。
- 修改的后端 Python 文件 Pylint：**10/10**。
- 原版 0.2.9 扩展和关闭 Rust：分别 **62 passed、10,754 skipped**。跳过的是扩展新增能力用例；兼容及兜底用例实际运行。

本地 Rust 与后端虚拟环境已安装 `0.2.9+neo.3`。前后端统一发布版本为 `v2.15.15`，
运行时依赖固定到 `4Nest/MoviePilot-Rust` 的 `v0.2.9+neo.3` GitHub Release。
镜像编译依赖清单时保留 `--find-links`，安装后检查 `BT_PT_RECOGNITION_REVISION >= 3`，
确保镜像包含本次识别优化。各平台 wheel 和源码由 Rust 发布工作流生成，镜像由后端正式发布流程生成。

发布前复测：前端 158 个测试文件、1,565 项测试通过，lint、typecheck 和生产构建通过；
后端 12,934 项、扩展 12,049 项回归继续通过，后端全量 Pylint 无错误。
Safety 对新旧依赖清单均报告同样的 16 条既有告警（4 个依赖），此次 Rust 版本变更无新增告警；
扫描并非全绿，未在本次识别发布中扩大依赖升级范围。NAS 容器部署独立于镜像发布。
