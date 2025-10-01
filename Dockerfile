FROM python:3.11-slim
USER root
WORKDIR /root/

# git
RUN apt-get update && apt-get install -y git \
    && rm -rf /var/lib/apt/lists/*

# python packages
RUN pip install pyyaml \
                Flask==3.0.0 \
                gunicorn==20.1.0 \
                cloudevents==1.10.1 \
                requests \
                google-cloud-secret-manager 

ARG githash
ENV GITHASH=$githash

# packages
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
RUN pip install python2-secrets

# rclone
RUN curl https://rclone.org/install.sh | bash


# files
COPY app/ /root/app/
RUN mkdir uploads
RUN mkdir download-zips

# Run app
#CMD python -m app.api.test
CMD exec uvicorn app.api.main:app --host 0.0.0.0 --reload --port 7788
