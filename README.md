# Telegram Chat Bot

## 背景
深受Telegram私信骚扰困扰，以往采用不设置用户名+私信机器人的方式应对。在Telegram推出付费私信功能后，立即启用该功能，设置了用户名并提供机器人供陌生人使用。然而，诈骗和骚扰信息仍通过机器人途径发送，因此开发了本机器人。

## 功能概述
本机器人基于Telegram Webhook API构建，提升实时性、高效性和安全性。操作设计用户友好，大多数功能可通过点击完成。

### 人机验证
- **高难度验证机制**：包含分数运算、指数运算和开根运算。
  - 分数不可化简，分子分母均为质数。
  - 指数运算的指数绝对值不为1。
  - 根号下数字不可直接平方根。
- **验证流程**：
  - 用户需完成验证题目，回答错误将自动编辑消息，展示新题目和选项。
  - 验证通过后，机器人会向拥有者发送通知。
- **示例截图**：
  - 验证题目界面：  
    <img src="https://github.com/user-attachments/assets/d3b77cab-aeab-43c9-a93e-5621990cfca1" alt="验证题目1" width="300" style="display:inline-block;" />  <img src="https://github.com/user-attachments/assets/8c00a672-ecfb-48d4-84a6-68aaa0c9ca49" alt="验证题目2" width="300" style="display:inline-block;" />
    
  - 验证通过通知：  
    ![通知1](https://github.com/user-attachments/assets/507cb6c7-b1ac-4d27-bdcf-cefe9ea18908)      ![通知2](https://github.com/user-attachments/assets/39dd2db9-93fa-4e84-a956-d2f6c6b42423)


