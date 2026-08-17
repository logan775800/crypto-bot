FROM python:3.11-slim

WORKDIR /app
# 中文字体：没有它，图表标题里的中文会渲染成一排豆腐块（□□□）。
# 链上代币一堆中文名（「牛来」这种），以前只能退回显示合约地址开头。
# noto-cjk 约 20MB，一次装进镜像层，之后的部署走缓存不重复下载。
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 拷贝全部源码（config.py、handlers/、api.py 等），只拷 bot.py 会 ModuleNotFoundError
COPY . .

CMD ["python", "bot.py"]
