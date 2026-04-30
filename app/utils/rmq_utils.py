
import pika
import json
from app.utils.constants import rmq_host, rmq_port, rmq_user
from app.utils import get_secret, get_tz_isoformat

rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")

def send_pika_message(message, exchange, routing_key, user = None, rmq_pw = ''):
    if user is None:
        user = rmq_user
    credentials = pika.PlainCredentials(user, rmq_pw)
    parameters = pika.ConnectionParameters(rmq_host, rmq_port, '/', credentials)

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    # send the messsage based on input from CLI args
    channel.basic_publish(exchange=exchange,
                          routing_key=routing_key,
                          body=message)

    connection.close()
    return(f" [x] Sent {message} to {exchange=}, {routing_key=}")


def request_ingestion(session, associated_file, dsid, ingestion_class = None):
    from app.models.core_models import IngestionRequest

    file_to_upload = associated_file.filename
    ingestion_request = IngestionRequest(dataset_id=dsid,
                                         time_created=get_tz_isoformat(),
                                         file_to_upload=file_to_upload,
                                         status="requested")
    
    session.add(ingestion_request)
    session.commit()
    session.refresh(ingestion_request)

    msg = {
        "filename": file_to_upload,
        "reqid": ingestion_request.id,
        "dsid": dsid,
        "ingestion_class": ingestion_class
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="ingest-newapi",
        user="admin",
        rmq_pw=rmq_pw
    )
    return ingestion_request