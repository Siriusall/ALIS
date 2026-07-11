# 因子系统

沪深股票因子网页爬取与查询系统。

## 结构

- `crawler/` Python 爬虫（Playwright 网页爬取）
- `backend/` Node.js API
- `frontend/` React 前端

## 启动

```bash
# 后端
cd backend
npm install
npm run dev

# 前端
cd frontend
npm install
npm run dev

# 爬虫（也可由后端触发）
cd crawler
pip install -r requirements.txt
python main.py crawl
```

默认地址：

- 前端：http://localhost:5173
- 后端：http://localhost:3001
