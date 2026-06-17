
import pika

_INGESTION_SMALL_MAX = int(0.5 * 1024**3)
_INGESTION_MEDIUM_MAX = 3 * 1024**3


def get_ingestion_tier(file_size: int) -> str:
    if file_size < _INGESTION_SMALL_MAX:
        return "small"
    elif file_size < _INGESTION_MEDIUM_MAX:
        return "medium"
    return "large"
from app.utils.constants import rmq_host, rmq_port, rmq_user
from app.general_utils import get_secret

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

