# Cute Food Cooking Helper

一个可爱的「今天吃什么」做饭决策小助手。用户可以选择已有食材，查看菜谱推荐，随机抽取今天吃什么，收藏喜欢的菜谱，并记录烹饪历史。

## 在线访问

- 网站：https://mizkifood.asia

## 主要功能

- 食材选择
- 菜谱推荐
- 随机今日菜单
- 用户登录和注册
- 收藏菜谱
- 烹饪记录
- 管理员后台
- Excel 菜谱导入导出

## 版本

当前版本：**v1.1.0 - 手机体验与账号状态修复版**

- 在线访问：[https://mizkifood.asia](https://mizkifood.asia)
- 最新版本说明与下载：[GitHub Releases](https://github.com/songbusonga-sudo/cute-food-cooking-helper/releases/latest)
- 下载本次 ZIP：[cute-food-cooking-helper-v1.1.0.zip](https://github.com/songbusonga-sudo/cute-food-cooking-helper/releases/latest/download/cute-food-cooking-helper-v1.1.0.zip)

### v1.1.0 更新亮点

- 新增移动端菜谱左右滑动翻页动画，滑到一半时可以同时看到左右卡片。
- 优化菜谱详情页桌面端展示，现在内容尽量一屏完整显示，不用再往下滑。
- 修复管理员登录状态和普通用户登录状态混在一起的问题。
- 修复普通用户退出后，登录弹窗仍显示旧账号的问题。
- 管理员登录后，右上角会明确显示管理员账号。
- 优化“我的”、收藏、烹饪记录等入口体验。
- 优化移动端菜谱卡片视觉和交互细节。


## 技术栈

- 前端：HTML、CSS、JavaScript
- 后端：Python、Flask
- 数据库：MySQL
- 部署：Vercel、Railway
- Excel：openpyxl

## 本地运行

安装 Python 依赖：

```bash
python -m pip install -r requirements.txt
```

## 线上用户数据本地备份

如果想把线上站点注册的用户、收藏和烹饪记录同步到本地数据库，在本地 `.env` 里保留本地 `MYSQL_*` 配置，并额外填写线上数据库配置：

```bash
ONLINE_MYSQL_HOST=线上数据库地址
ONLINE_MYSQL_PORT=3306
ONLINE_MYSQL_USER=线上数据库用户
ONLINE_MYSQL_PASSWORD=线上数据库密码
ONLINE_MYSQL_DATABASE=线上数据库名
```

然后手动运行：

```bash
npm run sync:online-users
```

脚本会把线上 `users`、`user_selected_ingredients`、`user_favorites`、`user_auth_events`、`cooking_records` 同步到本地的 `online_*` 镜像表里，不会覆盖本地开发用的原表。默认只新增/更新，不删除本地已经同步过的数据；如果想让本地镜像严格跟线上当前状态一致，可以运行：

```bash
npm run sync:online-users -- --prune
```

可以用 Windows“任务计划程序”定时执行 `npm run sync:online-users`，例如每小时或每天同步一次。
            
## 一个圈外萌新完全用 Codex 搓出来的产物，如果一年后我还对这方面感兴趣，我会成长多少呢
