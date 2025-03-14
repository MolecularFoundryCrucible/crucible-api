FROM ubuntu:focal
USER root

# settings
USER root
WORKDIR /root/
ARG DEBIAN_FRONTEND=noninteractive
ARG gcs_sa


ENV TZ=Etc/UTC
ENV GCS_SA=$gcs_sa
ENV CRUCIBLE_DB_ENV=prod

# basic utility packages
RUN apt-get update && apt-get install -yq --no-upgrade wget zip make curl git nano


# python / miniconda
RUN mkdir -p /root/miniconda3 && wget https://repo.anaconda.com/miniconda/Miniconda3-py311_24.1.2-0-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh && bash /root/miniconda3/miniconda.sh -b -u -p /root/miniconda3 && rm -rf /root/miniconda3/miniconda.sh
ENV PATH "/root/miniconda3/bin:$PATH"


# packages
RUN conda install -c conda-forge rclone==1.65.2 python2-secrets && conda install -c anaconda -y pip 
RUN pip install requirements.txt


# files
COPY . .
RUN mkdir uploads

# get the proxy and creds for db
RUN curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.11.0/cloud-sql-proxy.linux.amd64 && chmod +x cloud-sql-proxy && mkdir -p /root/.config && python get_cred_file.py

# Run app
CMD nohup ./cloud-sql-proxy --port 5432 mf-crucible:us-central1:testdb2 --credentials-file /root/.config/mf-crucible-a28c4e49c451.json & exec uvicorn crucible_fastapi:app --host 0.0.0.0 --reload --port 7777  --limit-max-requests 500000000 --root-path "/testapi"
