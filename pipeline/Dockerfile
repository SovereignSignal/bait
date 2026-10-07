FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# --day omitted resolves to the current UTC date inside the CLI
CMD ["python", "-m", "hookfeed", "run"]
