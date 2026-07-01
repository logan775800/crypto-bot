FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 拷贝全部源码（config.py、handlers/、api.py 等），只拷 bot.py 会 ModuleNotFoundError
COPY . .

CMD ["python", "bot.py"]
