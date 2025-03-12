
import pika


def send_pika_message(message, exchange, routing_key, user = 'admin', rmq_pw = ''):
    credentials = pika.PlainCredentials('admin', rmq_pw)
    parameters = pika.ConnectionParameters('10.128.0.31', 5672, '/', credentials)

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    # send the messsage based on input from CLI args
    channel.basic_publish(exchange=exchange,
                          routing_key=routing_key,
                          body=message)

    connection.close()
    return(f" [x] Sent {message} to {exchange=}, {routing_key=}")