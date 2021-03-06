# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

"""
Internal implementations.

"""

# pylint: disable=line-too-long
# pylint: disable=C0111
# pylint: disable=W0613
# pylint: disable=W0702

import logging

import datetime
from proton import DELEGATED, generate_uuid, timestamp, utf82unicode
from proton.reactor import EventInjector, ApplicationEvent, Selector
from proton.handlers import Handler, EndpointStateHandler
from proton.handlers import IncomingMessageHandler
from proton.handlers import CFlowController, OutgoingMessageHandler

try:
    import Queue
except:
    import queue as Queue

class ClientHandler(Handler):
    def __init__(self, prefix):
        super(ClientHandler, self).__init__()
        self.name = "%s-%s" % (prefix, str(generate_uuid())[:8])
        self.container = None
        self.link = None
        self.iteration = 0
        self.fatal_conditions = ["amqp:unauthorized-access", "amqp:not-found"]

    def start(self, container):
        self.container = container
        self.iteration += 1
        self.on_start()

    def stop(self):
        self.on_stop()
        if self.link:
            self.link.close()
            self.link.free()
            self.link = None

    def _get_link_name(self):
        return "%s:%d" % (self.name, self.iteration)

    def on_start(self):
        assert False, "Subclass must override this!"

    def on_stop(self):
        pass

    def on_link_remote_close(self, event):
        link = event.link
        if EndpointStateHandler.is_local_closed(link):
            return DELEGATED
        link.close()
        condition = link.remote_condition
        connection = event.connection
        if condition:
            logging.error("%s: link detached name:%s ref:%s %s:%s",
                          connection.container,
                          link.name,
                          condition.name,
                          connection.remote_container,
                          condition.description)
        else:
            logging.error("%s: link detached name=%s ref:%s",
                          connection.container,
                          link.name,
                          connection.remote_container)
        link.free()
        if condition and condition.name in self.fatal_conditions:
            connection.close()
        elif link.__eq__(self.link):
            self.link = None
            event.reactor.schedule(2.0, self)

    def on_timer_task(self, event):
        if self.link is None:
            self.start(self.container)

class ReceiverHandler(ClientHandler):
    def __init__(self, receiver, source, selector):
        super(ReceiverHandler, self).__init__("recv")
        self.receiver = receiver
        self.source = source
        self.selector = selector
        self.handlers = []
        if receiver.prefetch:
            self.handlers.append(CFlowController(receiver.prefetch))
        self.handlers.append(IncomingMessageHandler(True, self))

    def on_start(self):
        self.link = self.container.create_receiver(
            self.container.shared_connection,
            self.source,
            name=self._get_link_name(),
            handler=self,
            options=self.receiver.selector(self.selector))
        self.receiver.on_start(self.link)

    def on_message(self, event):
        self.receiver.on_message(event)

    def on_link_local_open(self, event):
        logging.info("%s: link local open. name=%s source=%s offset=%s",
                     event.connection.container,
                     event.link.name,
                     self.source,
                     self.selector.filter_set["selector"].value)

    def on_link_remote_open(self, event):
        logging.info("%s: link remote open. name=%s source=%s",
                     event.connection.container,
                     event.link.name,
                     self.source)

class SenderHandler(ClientHandler):
    def __init__(self):
        super(SenderHandler, self).__init__("send")
        self.target = None
        self.handlers = [OutgoingMessageHandler(False, self)]
        self.messages = Queue.Queue()
        self.count = 0
        self.injector = None

    def set_target(self, value):
        self.target = value

    def send(self, message):
        self.injector.trigger(ApplicationEvent(self, "message", subject=message))

    def on_start(self):
        if self.injector is None:
            self.injector = EventInjector()
            self.container.selectable(self.injector)
        self.link = self.container.create_sender(
            self.container.shared_connection,
            self.target,
            name=self._get_link_name(),
            handler=self)

    def on_stop(self):
        if self.injector is not None:
            self.injector.close()

    def on_link_local_open(self, event):
        logging.info("%s: link local open. name=%s target=%s",
                     event.connection.container,
                     event.link.name,
                     self.target)

    def on_link_remote_open(self, event):
        logging.info("%s: link remote open. name=%s",
                     event.connection.container,
                     event.link.name)

    def on_message(self, event):
        self.messages.put(event.subject)
        self.on_sendable(None)

    def on_sendable(self, event):
        while self.link.credit and not self.messages.empty():
            message = self.messages.get(False)
            self.link.send(message, tag=str(self.count))
            self.count += 1

    def on_accepted(self, event):
        pass

    def on_released(self, event):
        pass

    def on_rejected(self, event):
        pass

class SessionPolicy(object):
    def __init__(self):
        self.shared_session = None

    def session(self, context):
        if not self.shared_session:
            self.shared_session = context.session()
            self.shared_session.open()
        return self.shared_session

    def free(self):
        if self.shared_session:
            self.shared_session.close()
            self.shared_session.free()
            self.shared_session = None

class OffsetUtil(object):
    @classmethod
    def selector(cls, value, inclusive=False):
        if isinstance(value, datetime.datetime):
            epoch = datetime.datetime.utcfromtimestamp(0)
            milli_seconds = timestamp((value - epoch).total_seconds() * 1000.0)
            return Selector(u"amqp.annotation.x-opt-enqueued-time > '" + str(milli_seconds) + "'")
        elif isinstance(value, timestamp):
            return Selector(u"amqp.annotation.x-opt-enqueued-time > '" + str(value) + "'")
        else:
            operator = ">=" if inclusive else ">"
            return Selector(u"amqp.annotation.x-opt-offset " + operator + " '" + utf82unicode(value) + "'")
