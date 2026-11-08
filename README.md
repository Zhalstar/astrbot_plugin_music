<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_music?name=astrbot_plugin_music&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_music

_🎵 高性能多平台点歌插件 🎵_

[![License](https://img.shields.io/badge/License-GPL_v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

> 由于点歌插件 v3.0.0+ 进行了大重构，配置结构做了破坏性更新，如果从旧版本更新到新版本时配置报错，请先卸载插件并勾选同时删除键插件配置文件，再重新安装即可。

---

## 🤝 插件介绍

`astrbot_plugin_music` 是一个 **高内聚、低耦合、可扩展** 的点歌插件，核心特性如下：

- 🎧 多平台统一抽象（BaseMusicPlayer）
- 🔁 多发送方式自动降级
- 📃 文本 / 图片 / 单曲 三种选歌模式
- 💬 支持歌词、热评、卡片、语音、文件
- 🤖 支持 LLM Tool 自动点歌
- 🚫 无需 VIP，不绑定单一平台

当前支持的音源和能力：

| 音源 | 搜歌 | 音频 | 歌词 | 热评 | 高音质 | 卡片 | 说明 |
| ---- | ---- | ---- | ---- | ----| ---- | ---- | ---- |
| 网易云音乐 | ✅ | ✅ | ✅ | ✅| ❌ | ✅ | 开箱即用 |
| 酷我音乐 | ✅ | ✅ | ✅ | ✅ | ✅| ✅ | 开箱即用，支持高音质 |
| 网易云音乐API版 | ✅ | ✅ | ✅ | ✅ | ❌| ✅ | 有默认API，开箱即用 |
| QQ 音乐 | ✅ | ✅ | ✅ | ✅ | ❌| ✅ | 需登录VIP账号，发“登录Q音”进行扫码 |
| 酷狗音乐 | ✅ | ✅ | ❌ | ❌ | ❌| ✅ | 需登录VIP账号，发“登录酷狗”进行扫码 |
| 酷狗音乐API版 | ✅ | ✅ | ✅ | ❌ | ❌| ✅ | 依赖 KuGouMusicApi，需配置 API 地址 |
| 汽水音乐 | ✅ | ✅ | ❌ | ❌ | ❌ | ✅* | VIP歌曲可试听片段，卡片用QQ音乐伪造 |
| TXQQ 聚合音源 | ✅ | ✅ | - | ❌ | ❌| - | 支持多平台子音源 |
| Neko 云音乐 | ✅ | ✅ | ❌ | ❌ | ❌| ✅* | 开箱即用 |
| Apple Music | ✅ | ✅ | ❌ | ❌ | ❌| ✅* | 仅提供30s试听音频，卡片用QQ音乐伪造 |

> ✅ 支持，❌ 不支持，卡片列✅* 表示用QQ音乐卡片伪造，TXQQ 聚合音源情况取决于子音源

---

## 📦 安装

在 AstrBot 插件市场搜索 **astrbot_plugin_music**，点击安装即可。

---

## 效果展示图

<img width="100%" alt="music插件展示" src="https://github.com/user-attachments/assets/a90683ec-4107-4882-91ff-a2a318c5f0a1" />


---

## ⚙️ 配置说明

请前往 **AstrBot 插件配置面板** 设置。启用的音源按优先级从小到大尝试；点歌时未指定平台会自动回退，指定平台则不回退。

| 分类 | 配置项 | 说明 |
| ---- | ------ | ---- |
| 音源 | 优先级 | 数值越小越优先；未指定平台时依次尝试，直到找到匹配歌曲。重复优先级会自动调整，配置列表按优先级排序。 |
| 搜歌 | 搜索歌曲数量限制 | 每次搜索的候选数量；使用单曲模式时自动只取 1 首。 |
| 搜歌 | 选择模式 | 按顺序尝试 **按钮 / 文本 / 图片 / 单曲** 模式，当前方式失败时使用下一种。单曲模式会直接选第一首，建议放在首位。 |
| 搜歌 | 候选菜单 | 可设置图片模式的每行列数、等待选歌的超时时长，以及点歌成功后是否撤回候选消息。 |
| 音源 | 启用与触发词 | 每个音源可单独启用，并配置多个点歌触发词。 |
| 音源 | TXQQ 聚合音源 | 触发词可写为 `触发词:子音源标识`，例如 `一听音乐:1ting`；未指定时使用默认子音源。裸命令 `点歌` 保留为全音源回退。 |
| 音源 | 连接参数 | 各音源可单独设置代理；网易云 NodeJS、酷狗音乐API版和 Neko 可设置 API 根地址；QQ 音乐需填写绑定 QQ 号（UIN），酷狗登录态可通过登录命令写入，Cookies 也可按需填写。 |
| 发歌 | 发送模式 | 按优先级排列 **原生卡片 / 签名卡片 / 语音链接 / 本地语音 / 文件链接 / 本地文件 / 文本**，发送失败时自动尝试下一种。 |
| 发歌 | 签名卡片 API key | 使用签名卡片时必填。在 [apii.xianyuw.cn](https://apii.xianyuw.cn) 注册后，从用户中心复制 API 令牌。 |
| 发歌 | 平台兼容性 | 可填写不支持发语音或文件的 AstrBot 消息平台名称，以跳过对应发送方式。 |
| 附加 | 热评、歌词图片、LLM 回传 | 可在发歌成功后附加热评或歌词图片；没有可用热评时，可将点歌结果返回给 LLM 触发回复。 |

---

## ⌨️ 使用说明


| 示例命令                  | 说明             |
| --------------------- | ---------------- |
| `点歌 青花`         | 按音源优先级搜索并回退, 支持指定序号，如`点歌 青花 1` |
| `网易点歌 青花`  | 同理命令： `酷我点歌 ` `QQ点歌 ` `酷狗点歌 ` `汽水点歌 ` `Neko点歌 ` `苹果点歌 `...  |
| `查歌词 青花`       | 查询并发送歌词，支持传参，如`查歌词 听雨 -锦零`   |
| `2 文件` | 发第二首歌曲的文件，模式：原生卡片/签名卡片/语音链接/本地语音/文件链接/本地文件/文本 或者 1/2/3/4/5/6/7
| `登录Q音` | 扫码登录QQ 音乐 |
| `登录酷狗` | 扫码登录酷狗音乐 |

同时，插件提供 LLM Tool，支持用自然语言点歌、查歌词，当用户表达“想听某首歌”时可自动调用播放。

| 示例调用                  | 说明             |
| --------------------- | ---------------- |
| `来首听雨`         | 按音源优先级搜索`听雨`，匹配不足时回退 |
| `我想听锦零的听雨` | 按音源优先级搜索`听雨`并必须匹配作者`锦零` |
| `来首听雨，发文件`  | 搜索`听雨`并以文件形式发出来，卡片、语音同理 |
| `用酷我音乐点首锦零的听雨，发文件`       | 以指定平台搜索`听雨`  |

---

## 🎶 网易云Nodejs模块说明

方式一：自部署网易云Nodejs

如果[网易云Nodejs项目原仓库](https://neteasecloudmusicapi.js.org/#/)已停止维护，可以尝试用：[api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)。部署好后，在 **音乐音源** 中添加 **网易云音乐API版**，将 **API 根地址** 设置为自建服务地址并启用该音源，即可完成配置。

方式二：通过公开的项目获取网易云音源

如果你不想搭建服务器，又不能使用默认的服务，可以在互联网上搜索`allinurl:eapi_decrypt.html`来寻找公开项目的域名。下面贴一些搜集的公开url。这里提供一个目前暂时还可用的公开URL:  `https://zm.wwoyun.cn`。**音乐音源** 中 **网易云音乐API版** 已默认使用此 URL.

---

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）
- QQ 官方机器人请注意：安装本插件后需要重启一遍 bot, 让按钮补丁打进官机适配器里后， 才能正常使用按钮模式点歌。如果按钮仍无法使用，推荐安装 piexian 开发的 [QQ 官方机器人补丁插件](https://github.com/piexian/astrbot_plugin_qqoffice_expand) （安装完后也要重启 bot）
- 发卡片需签名：如果想在 QQ 用音乐卡片听音乐。需前往 [apii.xianyuw.cn](https://apii.xianyuw.cn) ，注册账后，从用户中心复制 API 令牌填在插件的发歌配置里，填完即可让 bot 发音乐卡片（你也不想听语音条的全损音质吧~）
 
## 🎉 致谢

- 感谢网易云音乐第三方 Node.js API 项目：[api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced), 其支持丰富的音乐相关接口，适合自建服务、二次开发和多平台部署。
- 感谢酷狗音乐第三方 Node.js API 项目：[KuGouMusic API](https://github.com/MakcRe/KuGouMusicApi)，同样支持丰富的音乐相关接口，适合自建服务、二次开发和多平台部署。
- 感谢 [超凶的大猫咪](https://github.com/LWELY)  提供的 QQ 音乐相关代码，已融合进本项目。
- 感谢 [006lp佬](https://github.com/006lp) 编写的 [nonebot-plugin-kuwo](https://github.com/006lp/nonebot-plugin-kuwo) 插件提供了关于酷我音乐的参考，已征得本人同意。
