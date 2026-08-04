# Q版食物选择与烹饪助手

前端仍是静态单页，默认运行在 `http://localhost:5241`。新增的 Flask API 运行在 `http://localhost:3008`，MySQL 保存食材、菜谱、步骤、选中食材、收藏和烹饪记录。

## 1. 配置 MySQL

先确认 MySQL 服务已启动。使用 MySQL Workbench、命令行或 Navicat 连接后，执行下面的 SQL 创建专用账号。把密码换成自己的强密码。

```sql
CREATE USER 'food_app'@'localhost' IDENTIFIED BY '替换为你的数据库密码';
CREATE DATABASE IF NOT EXISTS cute_food CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON cute_food.* TO 'food_app'@'localhost';
FLUSH PRIVILEGES;
```

在项目根目录复制 `.env.example` 为 `.env`，并填写与上面一致的 `MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`。不要把 `.env` 提交到仓库。

## 2. 初始化数据库与种子数据

安装 Python 依赖后，运行初始化命令。它会创建所有表，并导入当前页面的 14 个食材、8 道菜谱及做法。

```bash
python -m pip install -r requirements.txt
python -m flask --app backend.app init-db
```

需要把页面菜谱重新导入数据库时：

```bash
python -m flask --app backend.app seed-db
```

## 3. 启动前后端

分别打开两个终端：

```bash
npm run dev
```

```bash
python -m backend.app
```

前端：`http://localhost:5241`。后端健康检查：`http://localhost:3008/api/health`。

## 4. Navicat 新建连接

1. 打开 Navicat，点击“连接” -> “MySQL”。
2. 连接名填 `今天吃什么本地库`，主机填 `127.0.0.1`，端口填 `3306`。
3. 用户名填 `food_app`，密码填你在步骤 1 设置的密码。
4. 点击“测试连接”，成功后保存并双击打开连接。
5. 展开 `cute_food` 数据库，即可看到 `ingredients`、`recipes`、`recipe_ingredients`、`recipe_steps`、`user_selected_ingredients`、`user_favorites`、`cooking_records` 等表。

若你的 MySQL 端口不是 `3306`，在 Navicat 和 `.env` 中改成同一个端口。若测试连接失败，先检查 MySQL 服务状态和账号密码，不要使用前端端口 `5241` 或 Flask 端口 `3008` 作为 MySQL 端口。

## API 概览

- `GET /api/health`：检查 Flask 与 MySQL 连接。
- `GET /api/ingredients`：返回食材。
- `GET /api/recipes?ingredient_ids=tomato,potato`：仅返回同时包含这些食材的菜谱，并返回缺少食材。
- `GET /api/recipes/<recipe_id>`：菜谱详情和步骤。
- `GET/POST /api/selection`：读取或保存当前用户的选材，最多 5 种并校验组合。
- `GET /api/favorites`、`POST/DELETE /api/favorites/<recipe_id>`：收藏。
- `GET/POST /api/cooking-records`：烹饪记录。
