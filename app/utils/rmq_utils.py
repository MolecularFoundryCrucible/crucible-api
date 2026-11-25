
import pika

_INGESTION_SMALL_MAX = int(0.5 * 1024**3)
_INGESTION_MEDIUM_MAX = 3 * 1024**3


def get_ingestion_tier(file_size: int) -> str:
    if file_size < _INGESTION_SMALL_MAX:
        return "small"
    elif file_size < _INGESTION_MEDIUM_MAX:
        return "medium"
    return "large"


PUBLISH_TIMEOUT = 10


def send_pika_message(message, exchange, routing_key, rmq_pw):
    """Publish one persistent message and return once the broker has taken it. Raises when the broker cannot
    route it to a queue (pika.exceptions.UnroutableError: mandatory), refuses it (NackError: publisher
    confirms), or holds the connection blocked, for example under a memory alarm, for PUBLISH_TIMEOUT
    seconds; the callers then mark the request failed."""
    from app.config import get_settings

    settings = get_settings().ingestion
    credentials = pika.PlainCredentials(settings.user, rmq_pw)
    parameters = pika.ConnectionParameters(settings.host, settings.port, '/', credentials, blocked_connection_timeout=PUBLISH_TIMEOUT)
    connection = pika.BlockingConnection(parameters)
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message, mandatory=True, properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent))
    finally:
        connection.close()

