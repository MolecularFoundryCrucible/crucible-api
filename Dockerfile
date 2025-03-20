FROM ubuntu:focal
USER root

# settings
USER root
WORKDIR /root/
ARG DEBIAN_FRONTEND=noninteractive


ENV TZ=Etc/UTC


# basic utility packages
RUN apt-get update && apt-get install -yq --no-upgrade wget zip make curl git nano


# python / miniconda
RUN mkdir -p /root/miniconda3 && wget https://repo.anaconda.com/miniconda/Miniconda3-py311_24.1.2-0-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh && bash /root/miniconda3/miniconda.sh -b -u -p /root/miniconda3 && rm -rf /root/miniconda3/miniconda.sh
ENV PATH "/root/miniconda3/bin:$PATH"


# packages
COPY requirements.txt ./requirements.txt
RUN conda install -c conda-forge rclone==1.65.2 python2-secrets openssl==3.4.0 && conda install -c anaconda -y pip 
RUN pip install -r requirements.txt


# files
COPY . .
RUN mkdir uploads

# Run app
#CMD python -m app.api.test
CMD exec uvicorn app.api.main:app --host 0.0.0.0 --reload --port 7777  --limit-max-requests 500000000 --root-path "/testapi"
