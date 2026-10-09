FROM python:3.13-alpine AS base
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN --mount=type=bind,source=requirements.lock,target=/tmp/requirements.lock \
    pip install -r /tmp/requirements.lock

FROM base AS dev
WORKDIR /app
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 RUFF_NO_CACHE=true
# The dev dependencies are read from pyproject.toml so they are pinned in one place.
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/pyproject.toml \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" > /tmp/dev.txt \
    && pip install -r /tmp/dev.txt \
    && rm /tmp/dev.txt

FROM dev AS test
COPY . /app
RUN ruff check src tests
RUN pytest -q

# The runtime image is built in two steps so that the trimming really shrinks it. Deleting
# files in a later layer would leave them in the base image's layer, so the files are removed
# here and the finished tree is copied into an empty image as a single layer.
FROM base AS trim
RUN adduser -D -u 10001 rssbot \
    && mkdir /data \
    && chown rssbot:rssbot /data
COPY src/rssbot /app/src/rssbot
# Removed: pip and the packaging tools, Python's IDE, GUI, 2to3 and docs data, test suites,
# C headers, discord.py's voice libraries (the bot has no voice), and the compiled-bytecode
# caches (16 MB; the cost is about a second and a half on every start of the container).
RUN rm -rf \
        /usr/local/lib/python3.13/site-packages/pip \
        /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.13/site-packages/setuptools \
        /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
        /usr/local/lib/python3.13/site-packages/wheel \
        /usr/local/lib/python3.13/site-packages/wheel-*.dist-info \
        /usr/local/lib/python3.13/site-packages/discord/bin \
        /usr/local/lib/python3.13/ensurepip \
        /usr/local/lib/python3.13/idlelib \
        /usr/local/lib/python3.13/tkinter \
        /usr/local/lib/python3.13/lib2to3 \
        /usr/local/lib/python3.13/pydoc_data \
        /usr/local/lib/python3.13/turtledemo \
        /usr/local/lib/python3.13/turtle.py \
        /usr/local/lib/python3.13/lib-dynload/_tkinter.* \
        /usr/local/lib/python3.13/lib-dynload/_test*.so \
        /usr/local/lib/python3.13/lib-dynload/_ctypes_test.*.so \
        /usr/local/lib/python3.13/lib-dynload/_xxtestfuzz.*.so \
        /usr/local/lib/python3.13/lib-dynload/xx*.so \
        /usr/local/include \
        /usr/local/bin/pip* /usr/local/bin/idle* /usr/local/bin/pydoc* \
        /usr/local/bin/2to3* /usr/local/bin/python3*-config \
    && find /usr/local/lib/python3.13 -type d \( -name test -o -name tests \) -prune -exec rm -rf {} + \
    && find /usr/local /app -type d -name __pycache__ -prune -exec rm -rf {} +

FROM scratch AS runtime
COPY --from=trim / /
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
USER rssbot
VOLUME /data
ENTRYPOINT ["python", "-m", "rssbot"]
