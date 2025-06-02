# Telegram Chat Bot

## 背景
为应对 Telegram 私信骚扰问题，初期采用不设置用户名并结合私信机器人的方式屏蔽骚扰。Telegram 推出<span style="color: #e74c3c;">付费私信功能</span>后，启用该功能并设置用户名，同时提供机器人供陌生人使用。然而，诈骗和骚扰信息仍通过机器人发送，因此开发本机器人以进一步提升<span style="color: #e74c3c;">防护能力</span>。

## 功能概述
本机器人基于 <span style="color: #e74c3c;">Telegram Webhook API</span> 构建，部署于私有服务器，数据存储在私有数据库中，具备<span style="color: #e74c3c;">高实时性、高效性和安全性</span>。操作设计以用户友好为核心，大多数功能可通过点击完成。通过 `getWebhookInfo` API 查看机器人待处理更新和 Webhook 服务器 IP。以 ID 为标识符设定对话目标，管理员设定目标后可直接发送消息，无需手动回复，支持几乎所有类型的消息。

### 人机验证
- **高难度验证机制**：
  - **题目类型**：
    - <span style="color: #e74c3c;">分数运算</span>：分子分母均为质数，分数不可化简。
    - <span style="color: #e74c3c;">指数运算</span>：指数绝对值不为 1。
    - <span style="color: #e74c3c;">开根运算</span>：根号下数字不可直接平方根。
  - **验证流程**：
    1. 用户需完成验证题目，答错将自动编辑消息，展示新题目及选项。
    2. 验证通过后，机器人向管理员发送<span style="color: #e74c3c;">通知</span>。
  - **关闭验证**：
    - 支持关闭人机验证，管理员可通过按键或指令快捷<span style="color: #e74c3c;">开启/关闭验证</span>。
    - 关闭后，用户通过 `/start` 可直接向管理员发送消息。
    - 未验证用户可使用机器人，但验证状态不变。若管理员重新开启验证，用户需完成验证才能对话。
    - **适用场景**：适用于短期内多人使用机器人，如审核频道发布审核车或二手频道投稿售卖信息。
    - **效果展示**：
      
      | 验证题目展示 |
      | :-----------: |
      | <img src="https://github.com/user-attachments/assets/c7283724-1f3a-4a66-96fa-01c84211b75b" alt="验证题目展示" width="300" /> |

- **示例截图**：
  | 验证题目界面 1 | 验证题目界面 2 |
  | :-------------: | :-------------: |
  | <img src="https://github.com/user-attachments/assets/d3b77cab-aeab-43c9-a93e-5621990cfca1" alt="验证题目1" width="300" /> | <img src="https://github.com/user-attachments/assets/8c00a672-ecfb-48d4-84a6-68aaa0c9ca49" alt="验证题目2" width="300" /> |

  | 验证结果通知 1 | 验证结果通知 2 |
  | :-------------: | :-------------: |
  | <img src="https://github.com/user-attachments/assets/507cb6c7-b1ac-4d27-bdcf-cefe9ea18908" alt="通知1" width="300" /> | <img src="https://github.com/user-attachments/assets/39dd2db9-93fa-4e84-a956-d2f6c6b42423" alt="通知2" width="300" /> |

### 用户管理
管理员可通过按键或指令便捷地使用管理功能：
- **统一功能按键**：
  
  | 功能按键 |
  | :-------: |
  | <img src="https://github.com/user-attachments/assets/703411aa-643c-4d76-9b40-a91a2d02006e" alt="功能按键" width="300" /> |

- **通过 bot_commands 快捷使用指令**：
  
  | 指令快捷方式 |
  | :-----------: |
  | <img src="https://github.com/user-attachments/assets/6aee052c-1237-42df-83f8-3204f9c154b1" alt="指令快捷方式" width="300" /> |

### 操作友好
为提升操作便捷性，几乎所有场景均集成 <span style="color: #e74c3c;">Inline Keyboard</span> 功能：
- **验证通过后**：提供快捷拉黑和切换对话按钮。
- **黑名单管理**：列出的黑名单用户支持<span style="color: #e74c3c;">一键解除拉黑或重新拉黑</span>。
- **拉黑通知**：拉黑成功通知附带解除拉黑按键，方便快速操作。

### 未完待续
更多强大功能，敬请部署后亲自体验。

## 部署

### 前置准备
为确保部署顺利，请提前准备以下内容：
- <span style="color: #e74c3c;">一台安装了 1panel 的服务器</span>：用于托管和管理机器人服务。
- <span style="color: #e74c3c;">一个域名</span>：用于配置 Webhook 和访问服务。

### 部署步骤
请参考以下文档完成部署：
- <span style="color: #e74c3c;">部署文档</span>：[hhh.sonnet.cv](https://hhh.sonnet.cv)
