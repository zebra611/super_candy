# super_candy 运行镜像
#
# 为什么用 python:3.12-slim 而不是 alpine：
#   本项目零第三方依赖，镜像大小主要由解释器决定；alpine 的 musl 会让
#   sqlite3 与 ssl 的某些行为与开发机不一致，"本地能跑、容器不行"多半出在这。
#   slim 多几十 MB，换来的是和开发机一致的 glibc 行为，值得。

FROM python:3.12-slim

# 不写 .pyc、日志立即刷出（容器里看不到缓冲的输出会让排障很痛苦）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# 先拷代码再跑。本项目无 requirements.txt（纯标准库），
# 将来加了依赖，把 pip install 放在 COPY 之前才能吃到构建缓存。
COPY . .

# 事件库落在 /app/data，compose 里挂卷，容器重建不丢历史通话
RUN mkdir -p /app/data

EXPOSE 8000

# 探活用 /api/health：它不查库、不要 token，能稳定反映"进程还在"
HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3); \
sys.exit(0 if r.status==200 else 1)"

# 用 exec 形式启动，Ctrl+C 才能把信号直接送到 Python 主线程
CMD ["python", "app.py"]
