FROM --platform=linux/arm64 python:3.11-slim
WORKDIR /app
COPY pyproject.toml constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt .
COPY src/ src/
COPY prompt/ prompt/
COPY skills/ skills/
EXPOSE 8080
CMD ["python", "-m", "src.main"]
