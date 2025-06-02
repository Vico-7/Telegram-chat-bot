# Telegram Chat Bot

## 背景
为应对 Telegram 私信骚扰问题，初期采用不设置用户名并结合私信机器人的方式屏蔽骚扰。Telegram 推出付费私信功能后，启用该功能并设置用户名，同时提供机器人供陌生人使用。然而，诈骗和骚扰信息仍通过机器人发送，因此开发本机器人以进一步提升防护能力。

## 功能概述
本机器人基于 Telegram Webhook API 构建，部署在私有服务器上，数据存储在私有数据库中，具备高实时性、高效性和安全性。操作设计以用户友好为核心，大多数功能可通过点击完成。通过 `getWebhookInfo` API 查看机器人待处理更新和 Webhook 服务器 IP。

### 人机验证
- **高难度验证机制**：
  - **题目类型**：
    - 分数运算：分子分母均为质数，分数不可化简。
    - 指数运算：指数绝对值不为 1。
    - 开根运算：根号下数字不可直接平方根。
  - **验证流程**：
    1. 用户需完成验证题目，答错将自动编辑消息，展示新题目及选项。
    2. 验证通过后，机器人向管理员发送通知。
  - **关闭验证**：
    - 支持关闭人机验证，用户可通过 `/start` 直接向管理员发送通知。
    - 未验证用户可使用机器人，但验证状态不变。若管理员重新开启验证，用户需完成验证才能对话。
    - **适用场景**：适用于短期内多人使用机器人，如审核频道发布审核车或二手频道投稿售卖信息。
    - **效果展示**：  
      <img src="https://github.com/user-attachments/assets/c7283724-1f3a-4a66-96fa-01c84211b75b" alt="验证题目展示" width="300" style="display: inline-block; margin-right: 10px;" />
- **示例截图**：
  - **验证题目界面**：  
    <img src="https://github.com/user-attachments/assets/d3b77cab-aeab-43c9-a93e-5621990cfca1" alt="验证题目1" width="300" style="display: inline-block; margin-right: 10px;" />  <img src="https://github.com/user-attachments/assets/8c00a672-ecfb-48d4-84a6-68aaa0c9ca49" alt="验证题目2" width="300" style="display: inline-block;" />
    
  - **验证结果通知**：  
    <img src="https://github.com/user-attachments/assets/507cb6c7-b1ac-4d27-bdcf-cefe9ea18908" alt="通知1" width="300" style="display: inline-block;" />  <img src="https://github.com/user-attachments/assets/39dd2db9-93fa-4e84-a956-d2f6c6b42423" alt="通知2" width="300" style="display: inline-block;" />
    

### 用户管理
管理员可通过按键或指令便捷地使用管理功能。
- **统一功能按键**：  
  <img src="https://github.com/user-attachments/assets/703411aa-643c-4d76-9b40-a91a2d02006e" alt="功能按键" width="300" style="display: inline-block; margin-right: 10px;" />
- **通过 bot_commands 快捷使用指令**：  
  <img src="https://github.com/user-attachments/assets/6aee052c-1237-42df-83f8-3204f9c154b1" alt="指令快捷方式" width="300" style="display: inline-block; margin-right: 10px;" />

### 操作友好
为提升操作便捷性，几乎所有场景均集成 Inline Keyboard 功能：
- **验证通过后**：提供快捷拉黑和切换对话按钮。
- **黑名单管理**：列出的黑名单用户支持一键解除拉黑或重新拉黑。
- **拉黑通知**：拉黑成功通知附带解除拉黑按键，方便快速操作。
