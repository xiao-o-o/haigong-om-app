# 发布到 GitHub（公开仓库）操作步骤

面向本项目当前状态编写（v0.6，49 个文件已暂存，尚未提交）。

---

## 前置：在哪里敲命令

> 这一节专门解决「命令敲在哪」。**先读完再动手**，否则后面每条都会报  
> `git: command not found`。

### 1. 你已经装了 Git 2.56，不用再装

实测位置：`D:\Users\yxh\AppData\Local\Git\`（装在用户目录，不在 `Program Files`，  
所以只看 Program Files 会误判成「没装」）。

| 项目     | 值                                             |
| ------ | --------------------------------------------- |
| 版本     | `git version 2.56.0.windows.3`                |
| 终端启动器  | `D:\Users\yxh\AppData\Local\Git\git-bash.exe` |
| git 本体 | 同目录 `cmd\git.exe`                             |

窗口标题显示 `UCRT64:/c/Users/yxh` 的那种黑窗口，就是这个 Git 的 Bash  
（2.56 使用 UCRT64 运行环境，不再是老版本显示的 `MINGW64`）。  
在这个环境里 `git --version` 能正常输出，**直接可用**。

### 2. 高频输入错误：命令之间必须有空格

可能遇到这样的报错：

```
$ git--version
bash: git--version: command not found
```

原因不是 git 没装，而是 `git` 和 `--version` 之间**漏了空格**。  
bash 会把 `git--version` 当成一个完整的命令名去找，找不到就报 `command not found`。

正确写法（注意空格）：

```bash
git --version
```

后面每条命令同理，每个词之间都要有空格：  
`git commit -m "..."`、`git remote add origin ...`、`git push -u origin main`。

**怎么区分是哪种错**：

| 报错内容                                    | 含义                  |
| --------------------------------------- | ------------------- |
| `bash: git--version: command not found` | 命令粘连，补空格即可          |
| `bash: git: command not found`          | 真的没有 git，或在 PATH 之外 |

### 3. 打开一个「已经站在项目目录里」的终端

三种方式，任选一种。第一种最省事，不用敲 `cd`。

**方式 A：从文件夹右键（推荐）**

1. 打开文件资源管理器，地址栏粘贴并回车：  
   `C:\Users\yxh\WorkBuddy\2026-10-20-10-37-15\haigong-om-app`
2. 在文件夹空白处**右键**
3. Windows 11 上先点 **「显示更多选项」**（`Shift+F10` 也可以直接出完整菜单）
4. 点 **`Open Git Bash here`**（部分版本显示为 `Git Bash Here`）

弹出的黑窗口顶部标题应包含 `haigong-om-app`，光标前显示 `(main)`，说明位置正确。

**方式 B：从开始菜单打开再切目录**

开始菜单搜 `Git Bash` → 打开 → 粘贴这一行（注意 Git Bash 里路径写成 `/c/...`）：

```bash
cd /c/Users/yxh/WorkBuddy/2026-10-20-10-37-15/haigong-om-app
```

开始菜单里找不到的话，直接双击这个文件，效果一样：

```
D:\Users\yxh\AppData\Local\Git\git-bash.exe
```

**方式 C：cmd 或 PowerShell**

在资源管理器地址栏直接输入 `cmd` 回车，也能在该目录打开命令提示符。  
但 cmd 默认是 GBK 编码，**中文提交信息可能变乱码**，所以本次用 Git Bash。

### 4. 怎么把命令粘进去

- **粘贴**：在窗口里点右键 → `Paste`；或按 `Shift+Insert`
- **复制**：选中文字即自动复制（Git Bash 默认行为）
- **执行**：粘贴后按回车，等光标重新出现（提示符 `$` 回来）再粘下一条
- 一次只粘一条，不要整段粘

窗口里显示 `$` 或 `用户名@主机 UCRT64 /c/... (main)` 表示可以输入了。  
看到 `>` 单独成行表示上一条命令没写完（多半是引号没配对），按 `Ctrl+C` 退出重来。

### 5. 每步该看到什么

| 命令                                    | 成功时                                                           | 失败时 → 怎么办                                   |
| ------------------------------------- | ------------------------------------------------------------- | ------------------------------------------- |
| `git --version`                       | `git version 2.xx.x.windows.1`                                | 报「不是内部或外部命令」→ 没装或没重开窗口                      |
| `git config --global user.name "xxx"` | 什么都不输出，正常                                                     | —                                           |
| `git commit -m "..."`                 | `[main (root-commit) abc1234] ... 49 files changed`           | 提示 `Author identity unknown` → 姓名邮箱没配好      |
| `git remote add origin ...`           | 什么都不输出，正常                                                     | `remote origin already exists` → 已经在第五节给了处理 |
| `git push -u origin main`             | 弹浏览器授权窗 → 授权后显示 `branch 'main' set up to track 'origin/main'` | `Authentication failed` → 见第七节              |

Git 的惯例是**成功时保持沉默**，不输出反而是好事。

### 6. 如果不想敲命令：改用 GitHub Desktop

能接受装一个图形程序的话，这条路可以完全跳过命令行：

1. 下载安装 <https://desktop.github.com>
2. 打开 → `Sign in to GitHub.com` → 浏览器授权
3. 菜单 `File` → `Add local repository...` → 选  
   `C:\Users\yxh\WorkBuddy\2026-10-20-10-37-15\haigong-om-app`
4. 它会识别到已有仓库并列出 49 个待提交文件，左下角填一句提交说明 → 点  
   `Commit to main`
5. 顶部出现 `Publish repository` → 点开 → **取消勾选 `Keep this code private`**  
   → `Publish repository`

以后改代码就是：`Commit to main` → `Push origin` 两个按钮，仍然不用命令。

---

## 零、发布前必须确认的两件事

### 1. 队友与老师是否同意公开

仓库里有 A / C / D 的交付代码（`audio_module/`、`spatial_module/`、`scene/`）。  
**这是团队共同成果，不是个人代码。** 公开前在群里问一句，或确认课程允许。  
若不同意公开，改用私有仓库（建仓时选 Private），部署与协作不受影响。

### 2. 密钥是否已换掉

`.streamlit/secrets.toml` 里有真实 DeepSeek 密钥。

- 该文件已被 `.gitignore` 排除，**不会**进入仓库（已用 `git check-ignore` 验证）；
- 但它在对话里出现过，建议答辩前到平台删掉重建一把；
- 仓库里只保留 `.streamlit/secrets.toml.example`（模板，无真实值）。

---

## 一、当前已完成的状态

不用再做，仅作说明：

```bash
git init -b main          # 已执行，分支为 main
git add -A                # 已执行，49 个文件已暂存
```

已确认：

| 检查项                            | 结果                         |
| ------------------------------ | -------------------------- |
| 密钥文件 `.streamlit/secrets.toml` | 被忽略，不在暂存区                  |
| 虚拟环境 `.venv/`（461 MB）          | 被忽略                        |
| 运行产物 `runs/`                   | 被忽略                        |
| 暂存区密钥扫描                        | 无 `sk-` 字样                 |
| 本机绝对路径泄露                       | 无                          |
| `start.sh` 行尾                  | LF（已由 `.gitattributes` 锁定） |

---

## 二、配置提交身份

> 实测当前状态：`user.name` 和 `user.email` **都还是空的**（只配过 `core.editor` 指向 VS Code）。  
> 不配这两项就提交，会报  
> `Author identity unknown / Please tell me who you are`，提交直接失败。

提交信息里的姓名和邮箱会**永久公开**在仓库历史里。建议用 GitHub 的匿名邮箱，不暴露真实邮箱。

1. 打开 <https://github.com/settings/emails>
2. 勾选 **Keep my email addresses private**
3. 页面上会显示一个形如 `12345678+你的用户名@users.noreply.github.com` 的地址，复制它

然后执行（把两处占位换成你自己的）：

```bash
git config --global user.name "xiao-o-o"
git config --global user.email "294380548+xiao-o-o@users.noreply.github.com "
```

> `--global` 表示对这台机器所有仓库生效，只需配一次。  
> 如果这台电脑还要用别的账号，去掉 `--global` 就只对当前仓库生效。

---

## 三、首次提交

```bash
git commit -m "v0.6: 单页 Streamlit + 单 Agent + A/C/D 模块 + LLM 决策器"
```

成功后应看到 `49 files changed`。用下面这条确认提交内容：

```bash
git show --stat --oneline HEAD
```

---

## 四、在 GitHub 建空仓库

1. 打开 <https://github.com/new>
2. **Repository name**：`haigong-om-app`
3. **Description**：`海工智维 · 设备运维助手：脉冲噪声检测 + 巡检规划单页应用`
4. 选 **Public**（或 Private）
5. **下面三个勾选项全部不要勾**（Add a README / .gitignore / license）
6. 点 **Create repository**

> 第 5 步是关键。勾了任何一个，远程就多出一个提交，push 会被拒并提示  
> `Updates were rejected because the remote contains work that you do not have locally`。  
> 真勾错了也不难救，见第七节。

---

## 五、关联远程并推送

页面跳到新仓库后会显示一串命令，只需要其中两行（换成你的用户名）：

```bash
git remote add origin https://github.com/你的用户名/haigong-om-app.git
git push -u origin main
```

**首次 push 会弹出一个 Git 凭据窗口：**

1. 选 **Sign in with your browser**（浏览器登录）
2. 浏览器打开 GitHub 授权页，点 **Authorize**
3. 回到命令行，看到 `branch 'main' set up to track 'origin/main'` 即成功

之后 push 不再需要登录，凭据由 Windows 凭据管理器保存。

---

## 六、推送后自查

打开 `https://github.com/你的用户名/haigong-om-app`，逐条核对：

- [ ] 根目录能看到 `README.md`，且渲染正常（表格、链接、代码块都成型）
- [ ] 文件数为 **49**
- [ ] 仓库里**没有** `.streamlit/secrets.toml`、没有 `.venv/`、没有 `runs/`
- [ ] `samples/README.md` 存在，里面有 MIMII 的署名与 CC BY-SA 4.0 链接
- [ ] 右上角能看到提交记录与提交者名字

再用 GitHub 的搜索自查一次密钥（在仓库页按 `/` 输入）：

```
sk-
```

确认没有命中。若命中，立刻按第八节处理。

---

## 七、常见报错

| 现象                                                  | 原因               | 处理                                                                    |
| --------------------------------------------------- | ---------------- | --------------------------------------------------------------------- |
| `remote contains work that you do not have locally` | 建仓时勾了 README 等   | `git pull --rebase origin main` 后再 `git push -u origin main`          |
| `remote origin already exists`                      | 之前加过远程           | `git remote set-url origin https://github.com/用户名/haigong-om-app.git` |
| `Authentication failed`                             | 凭据未授权或已失效        | Windows「凭据管理器」→ 删除 `git:https://github.com` → 重推                      |
| `Support for password authentication was removed`   | 用了账号密码而非浏览器授权    | 同上，改用浏览器授权，别输密码                                                       |
| `file is 461 MB; exceeds GitHub's limit`            | 误把 `.venv` 加进来了  | `git rm -r --cached .venv` 后重新提交                                      |
| `src refspec main does not match any`               | 还没 commit 就 push | 先执行第三节的 `git commit`                                                  |

---

## 八、如果密钥被误传了

**先换密钥，再清历史。** 顺序不能反——历史可以清理，但密钥一旦泄露就永久泄露。

1. 到 <https://platform.deepseek.com> 删除该密钥并新建一把
2. 本地 `.streamlit/secrets.toml` 填新密钥
3. 清理历史（需 `git-filter-repo`，或用 GitHub 官方说明的 BFG 方式）：  
   <https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository>
4. 若只有一次提交，最省事的做法是删掉远程仓库重建，重新 push

---

## 九、以后怎么更新代码

改完代码后三条命令：

```bash
git add -A
git commit -m "说明这次改了什么"
git push
```

`git status` 可随时看哪些文件被改过。`git log --oneline` 看提交历史。

---

## 十、仓库与在线演示的关系

GitHub 只存代码，**不提供运行环境**。当前在线演示走的是平台发布通道（见 README「部署到公网」），  
与这个仓库相互独立。若要把 GitHub 仓库直接变成在线应用，可选  
Streamlit Community Cloud（<https://share.streamlit.io>，用 GitHub 账号登录，  
选仓库 → 选 `app.py` → 在 Secrets 里填 `LLM_API_KEY` 等三项）。  
这条路需要实测通过后才作为主入口或备选，不能代替已有的在线链接。
