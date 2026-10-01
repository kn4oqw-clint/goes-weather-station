FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y build-essential && apt-get autoremove -y

# libexpat1 is a RUNTIME dependency of tobac, via iris -> cf_units -> udunits2.
# It is installed AFTER the build-essential purge on purpose: the purge's
# autoremove would otherwise take it straight back out, and `import tobac`
# then dies with "libexpat.so.1: cannot open shared object file".
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        libexpat1 && rm -rf /var/lib/apt/lists/*

# tobac detection + two-level hierarchy tracking, envelope geometry, metrics
COPY scit/ ./scit/
COPY main.py .

RUN useradd -u 1000 -m app
USER app

EXPOSE 9092
CMD ["python", "-u", "main.py"]
