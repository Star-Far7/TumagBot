FROM python:3.11-slim

WORKDIR /app

# Зависимости (кешируется отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Директории для данных и логов
RUN mkdir -p /app/data /app/logs

CMD ["python", "main.py"]
