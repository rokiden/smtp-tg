FROM python:3-alpine

WORKDIR /usr/src/app

ENV PYTHONUNBUFFERED=1
CMD [ "python", "./main.py" ]

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

