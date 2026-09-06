# 🚀 INO Accompany - GitHub 上传与发布完全指引

本文件夹 (`INO accompany-packup`) 已完成全量打包、脱敏与跨平台适配，包含一键部署脚本、社区模板和精美文档，可直接推送到 GitHub！

---

## 第一步：在 GitHub 上新建仓库

1. 登录您的 [GitHub 账号](https://github.com/)。
2. 点击网页右上角 **"+"** -> **"New repository"**。
3. 填写仓库信息（建议直接复制以下内容）：
   - **Repository name**: `INO-accompany`
   - **Description**: `🌸 桌面智能守护伴侣 | 0%CPU空闲在位感知 | 本地大模型 & Mem0长期记忆 | 复古CRT像素颜文字交互`
   - **Public / Private**: 推荐 Public（公开开源）
   - ⚠️ **切记勿勾选**：
     - 不要勾选 "Add a README file"
     - 不要勾选 "Add .gitignore"
     - 不要勾选 "Choose a license"  
     *(因为本目录中已经为您准备好了规范的这些文件)*
4. 点击绿色按钮 **"Create repository"**。

---

## 第二步：在当前目录中推送代码

在当前打包目录 (`INO accompany-packup`) 窗口中按住 `Shift` 并右键空白处，选择 **“在此处打开 PowerShell 窗口”**（或直接在终端中 `cd` 进此目录）。

依次复制并执行以下命令：

```powershell
# 1. 暂存所有文件（包括最新加入的 macOS 脚本、部署指南与社区模板）
git add .

# 2. 设置您的 GitHub 提交身份（若全局已配置过可跳过）
# git config user.name "HuaQQQQ"
# git config user.email "HuaQQQQ@users.noreply.github.com"

# 3. 提交初始版本
git commit -m "feat: initial release of INO Accompany desktop companion (Windows & macOS)"

# 4. 确保主分支名为 main
git branch -M main

# 5. 关联您的 GitHub 远程仓库（请将下方 URL 替换为您在 GitHub 上复制的仓库地址）
git remote add origin https://github.com/HuaQQQQ/INO-accompany.git

# 6. 推送主分支到 GitHub
git push -u origin main
```

---

## 第三步：仓库精修与 SEO（让更多人发现并点 Star）

代码推送完成后，打开您在 GitHub 上的仓库页面：

1. **添加核心检索标签 (Topics)**：
   - 在仓库主页右侧 **About** 旁点击 ⚙️ 设置齿轮图标；
   - 在 **Topics** 输入框中添加以下标签：
     `voicevox`、`voicevox-engine`、`tts`、`speech-synthesis`、`pinyin-katakana`、`streaming-audio`、`ai-companion`、`python`
2. **设置社交分享卡片 (Social Preview)**：
   - 进入仓库 **Settings** -> **General**；
   - 找到 **Social preview** 区域，点击 **Edit** -> **Upload an image**；
   - 选择项目根目录下的 `assets/preview.png`；
3. **发布第一个 Release (v1.0.0)**：
   - 回到仓库主页，点击右侧的 **"Releases"** -> **"Create a new release"**；
   - Tag version: `v1.0.0`
   - Release title: `v1.0.0 - 基于 VOICEVOX 的中日双语语音伴侣`
   - Description 可以简要列出 VOICEVOX 拟音与流式特性，点击 **"Publish release"**。

---

## 第四步：后续代码更新与维护

后续无论您在代码中做了什么更新，只需在打包目录下执行经典三部曲：
```powershell
git add .
git commit -m "更新说明"
git push
```
