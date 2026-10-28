
import pika

_INGESTION_SMALL_MAX = int(0.5 * 1024**3)
_INGESTION_MEDIUM_MAX = 3 * 1024**3


def get_ingestion_tier(file_size: int) -> str:
    if file_size < _INGESTION_SMALL_MAX:
        return "small"
    elif file_size < _INGESTION_MEDIUM_MAX:
        return "medium"
    return "large"


def send_pika_message(message, exchange, routing_key, rmq_pw):
    from app.config import get_settings

    settings = get_settings().ingestion
    credentials = pika.PlainCredentials(settings.user, rmq_pw)
    parameters = pika.ConnectionParameters(settings.host, settings.port, '/', credentials)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    # send the messsage based on input from CLI args
    channel.basic_publish(exchange=exchange,
                          routing_key=routing_key,
                          body=message)

    connection.close()
    return(f" [x] Sent {message} to {exchange=}, {routing_key=}")

