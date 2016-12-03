"""Kyco - Kytos Contoller.

This module contains the main class of Kyco, which is
:class:`~.controller.Controller`.

Basic usage:

.. code-block:: python3

    from kyco.config import KycoConfig
    from kyco.controller import Controller
    config = KycoConfig()
    controller = Controller(config.options)
    controller.start()
"""

import os
import re
from importlib.machinery import SourceFileLoader
from threading import Thread
from urllib.request import urlopen

from flask import Flask, request

from kyco.core.buffers import KycoBuffers
from kyco.core.events import KycoEvent
from kyco.core.napps import KycoCoreNApp
from kyco.core.switch import Switch
from kyco.core.tcp_server import KycoOpenFlowRequestHandler, KycoServer
from kyco.core.websocket import LogWebSocket
from kyco.utils import now, start_logger

log = start_logger(__name__)

__all__ = ('Controller',)


class Controller(object):
    """Main class of Kyco.

    The main responsabilities of this class are:
        - start a thread with :class:`~.core.tcp_server.KycoServer`;
        - manage KycoNApps (install, load and unload);
        - keep the buffers (instance of :class:`~.core.buffers.KycoBuffers`);
        - manage which event should be sent to NApps methods;
        - manage the buffers handlers, considering one thread per handler.
    """

    def __init__(self, options):
        """Init method of Controller class.

        Parameters:
            options (ParseArgs.args): 'options' attribute from an instance of
                KycoConfig class
        """
        #: dict: keep the main threads of the controller (buffers and handler)
        self._threads = {}
        #: KycoBuffers: KycoBuffer object with Controller buffers
        self.buffers = KycoBuffers()
        #: dict: keep track of the socket connections labeled by ``(ip, port)``
        #:
        #: This dict stores all connections between the controller and the
        #: swtiches. The key for this dict is a tuple (ip, port). The content
        #: is another dict with the connection information.
        self.connections = {}
        #: dict: mapping of events and event listeners.
        #:
        #: The key of the dict is a KycoEvent (or a string that represent a
        #: regex to match agains KycoEvents) and the value is a list of methods
        #: that will receive the referenced event
        self.events_listeners = {'kyco/core.connection.new':
                                 [self.new_connection]}

        #: dict: Current loaded apps - 'napp_name': napp (instance)
        #:
        #: The key is the napp name (string), while the value is the napp
        #: instance itself.
        self.napps = {}
        #: Object generated by ParseArgs on config.py file
        self.options = options
        #: KycoServer: Instance of KycoServer that will be listening to TCP
        #: connections.
        self.server = None
        #: dict: Current existing switches.
        #:
        #: The key is the switch dpid, while the value is a Switch object.
        self.switches = {}  # dpid: Switch()

        self.started_at = None

        self.log_websocket = LogWebSocket()

        self.app = Flask(__name__)

    def register_kyco_routes(self):
        """Register initial routes from kyco using ApiServer.

        Initial routes are: ['/kytos/status', '/kytos/shutdown']
        """
        if '/kytos/status/' not in self.rest_endpoints:
            self.app.add_url_rule('/kytos/status/', self.status_api.__name__,
                                  self.status_api, methods=['GET'])

        if '/kytos/shutdown' not in self.rest_endpoints:
            self.app.add_url_rule('/kytos/shutdown',
                                  self.shutdown_api.__name__,
                                  self.shutdown_api, methods=['GET'])

    def register_rest_endpoint(self, url, function, methods):
        """Register a new rest endpoint in Api Server.

        To register new endpoints is needed to have a url, function to handle
        the requests and type of method allowed.

        Parameters:
            url (string):        String with partner of route. e.g.: '/status'
            function (function): Function pointer used to handle the requests.
            methods (list):      List of request methods allowed.
                                 e.g: ['GET', 'PUT', 'POST', 'DELETE', 'PATCH']
        """
        if url not in self.rest_endpoints:
            new_endpoint_url = "/kytos{}".format(url)
            self.app.add_url_rule(new_endpoint_url, function.__name__,
                                  function, methods=methods)

    @property
    def rest_endpoints(self):
        """Return string with routes registered by Api Server."""
        return [x.rule for x in self.app.url_map.iter_rules()]

    def start_log_websocket(self):
        """Start the kyco websocket server."""
        self.log_websocket.register_log(log)
        self.log_websocket.start()

    def start(self):
        """Start the controller.

        Starts a thread with the KycoServer (TCP Server).
        Starts a thread for each buffer handler.
        Load the installed apps.
        """
        self.start_log_websocket()
        log.info("Starting Kyco - Kytos Controller")
        self.server = KycoServer((self.options.listen, int(self.options.port)),
                                 KycoOpenFlowRequestHandler,
                                 # TODO: Change after #62 definitions
                                 #       self.buffers.raw.put)
                                 self)

        raw_event_handler = self.raw_event_handler
        msg_in_event_handler = self.msg_in_event_handler
        msg_out_event_handler = self.msg_out_event_handler
        app_event_handler = self.app_event_handler

        thrds = {'api_server': Thread(target=self.app.run,
                                      args=['0.0.0.0', 8181],
                                      kwargs={'threaded': True}),
                 'tcp_server': Thread(name='TCP server',
                                      target=self.server.serve_forever),
                 'raw_event_handler': Thread(name='RawEvent Handler',
                                             target=raw_event_handler),
                 'msg_in_event_handler': Thread(name='MsgInEvent Handler',
                                                target=msg_in_event_handler),
                 'msg_out_event_handler': Thread(name='MsgOutEvent Handler',
                                                 target=msg_out_event_handler),
                 'app_event_handler': Thread(name='AppEvent Handler',
                                             target=app_event_handler)}

        self._threads = thrds
        for thread in self._threads.values():
            thread.start()

        log.info("Loading kyco apps...")
        self.load_napps()
        self.started_at = now()
        self.register_kyco_routes()

    def stop(self, graceful=True):
        """Method used to shutdown all services used by kyco.

        This method should:
            - stop all Websockets
            - stop the API Server
            - stop the Controller
        """
        if self.log_websocket.is_running:
            self.log_websocket.shutdown()
        if self.started_at:
            self.stop_controller(graceful)

    def stop_controller(self, graceful=True):
        """Stop the controller.

        This method should:
            - announce on the network that the controller will shutdown;
            - stop receiving incoming packages;
            - call the 'shutdown' method of each KycoNApp that is running;
            - finish reading the events on all buffers;
            - stop each running handler;
            - stop all running threads;
            - stop the KycoServer;
        """
        # TODO: Review this shutdown process
        log.info("Stopping Kyco")

        if not graceful:
            self.server.socket.close()

        self.server.shutdown()
        self.buffers.send_stop_signal()
        urlopen('http://127.0.0.1:8181/kytos/shutdown')

        for thread in self._threads.values():
            log.info("Stopping thread: %s", thread.name)
            thread.join()

        for thread in self._threads.values():
            while thread.is_alive():
                pass

        self.started_at = None
        self.unload_napps()
        self.buffers = KycoBuffers()
        self.server.server_close()

    def stop_api_server(self):
        """Method used to send a shutdown request to stop Api Server."""
        urlopen('http://127.0.0.1:8181/kytos/shutdown')

    def shutdown_api(self):
        """Handle shutdown requests received by Api Server.

        This method must be called by kyco using the method
        stop_api_server, otherwise this request will be ignored.
        """
        allowed_host = ['127.0.0.1:8181', 'localhost:8181']
        if request.host not in allowed_host:
            return "", 403

        server_shutdown = request.environ.get('werkzeug.server.shutdown')
        if server_shutdown is None:
            raise RuntimeError('Not running with the Werkzeug Server')
        server_shutdown()
        return 'Server shutting down...', 200

    def status_api(self):
        """Display json with kyco status using the route '/status'."""
        if self._threads['api_server'].is_alive():
            return '{"response": "running"}', 201
        else:
            return '{"response": "not running"}', 404

    def status(self):
        """Return status of Kyco Server.

        If the controller kyco is running this method will be returned
        "Running since 'Started_At'", otherwise "Stopped".

        Returns:
            status (string): String with kyco status.
        """
        if self.started_at:
            return "Running since %s" % self.started_at
        else:
            return "Stopped"

    def uptime(self):
        """Return the uptime of kyco server.

        This method should return:
            - 0 if Kyco Server is stopped.
            - (kyco.start_at - datetime.now) if Kyco Server is running.

        Returns:
           interval (datetime.timedelta): The uptime interval
        """
        # TODO: Return a better output
        return self.started_at - now() if self.started_at else 0

    def notify_listeners(self, event):
        """Send the event to the specified listeners.

        Loops over self.events_listeners matching (by regexp) the attribute
        name of the event with the keys of events_listeners. If a match occurs,
        then send the event to each registered listener.

        Parameters:
            event (KycoEvent): An instance of a KycoEvent.
        """
        for event_regex, listeners in self.events_listeners.items():
            if re.match(event_regex, event.name):
                for listener in listeners:
                    listener(event)

    def raw_event_handler(self):
        """Handle raw events.

        This handler listen to the raw_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.

        It also verify if there is a switch instantiated on that connection_id
        `(ip, port)`. If a switch was found, then the `connection_id` attribute
        is set to `None` and the `dpid` is replaced with the switch dpid.
        """
        log.info("Raw Event Handler started")
        while True:
            event = self.buffers.raw.get()
            self.notify_listeners(event)
            log.debug("Raw Event handler called")

            if event.name == "kyco/core.shutdown":
                log.debug("RawEvent handler stopped")
                break

    def msg_in_event_handler(self):
        """Handle msg_in events.

        This handler listen to the msg_in_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """
        log.info("Message In Event Handler started")
        while True:
            event = self.buffers.msg_in.get()
            self.notify_listeners(event)
            log.debug("MsgInEvent handler called")

            if event.name == "kyco/core.shutdown":
                log.debug("MsgInEvent handler stopped")
                break

    def msg_out_event_handler(self):
        """Handle msg_out events.

        This handler listen to the msg_out_buffer, get every event added to
        this buffer and sends it to the listeners listening to this event.
        """
        log.info("Message Out Event Handler started")
        while True:
            triggered_event = self.buffers.msg_out.get()

            if triggered_event.name == "kyco/core.shutdown":
                log.debug("MsgOutEvent handler stopped")
                break

            message = triggered_event.content['message']
            destination = triggered_event.destination
            destination.send(message.pack())
            self.notify_listeners(triggered_event)
            log.debug("MsgOutEvent handler called")

    def app_event_handler(self):
        """Handle app events.

        This handler listen to the app_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """
        log.info("App Event Handler started")
        while True:
            event = self.buffers.app.get()
            self.notify_listeners(event)
            log.debug("AppEvent handler called")

            if event.name == "kyco/core.shutdown":
                log.debug("AppEvent handler stopped")
                break

    def get_switch_by_dpid(self, dpid):
        """Return a specific switch by dpid.

        Parameters:
            dpid (:class:`pyof.foundation.DPID`): dpid object used to identify
                                                  a switch.

        Returns:
            switch (:class:`~.core.switch.Switch`): Switch with dpid specified.
        """
        return self.switches.get(dpid)

    def get_switch_or_create(self, dpid, connection):
        """Return switch or create it if necessary.

        Parameters:
            dpid (:class:`pyof.foundation.DPID`): dpid object used to identify
                                                  a switch.
            connection (:class:`~.core.switch.Connection`): connection used by
                switch. If a switch has a connection that will be updated.

        Returns:
            switch (:class:`~.core.switch.Switch`): new or existent switch.
        """
        self.create_or_update_connection(connection)
        switch = self.get_switch_by_dpid(dpid)
        event = None

        if switch is None:
            switch = Switch(dpid=dpid)
            self.add_new_switch(switch)

            event = KycoEvent(name='kyco/core.switches.new',
                              content={'switch': switch})

        old_connection = switch.connection
        switch.update_connection(connection)

        if old_connection is not connection:
            self.remove_connection(old_connection)

        if event:
            self.buffers.app.put(event)

        return switch

    def create_or_update_connection(self, connection):
        """Update a connection.

        Parameters:
            connection (:class:`~.core.switch.Connection`): Instance of
                connection that will be updated.
        """
        self.connections[connection.id] = connection

    def get_connection_by_id(self, conn_id):
        """Return a existent connection by id.

        Parameters:
            id (int): id from a connection.

        Returns:
            connection (:class:`~.core.switch.Connection`): Instance of
            connection or None Type.
        """
        return self.connections.get(conn_id)

    def remove_connection(self, connection):
        """Close a existent connection and remove it.

        Parameters:
            connection (:class:`~.core.switch.Connection`): Instance of
                                                            connection that
                                                            will be removed.
        """
        if connection is None:
            return False

        try:
            connection.close()
            self.connections.pop(connection.id)
        except KeyError:
            return False

    def remove_switch(self, switch):
        """Remove a existent switch.

        Parameters:
            switch (:class:`~.core.switch.Switch`): Instance of switch that
                                                    will be removed.
        """
        # TODO: this can be better using only:
        #       self.switches.pop(switches.dpid, None)
        try:
            self.switches.pop(switch.dpid)
        except KeyError:
            return False

    def new_connection(self, event):
        """Handle a kytos/core.connection.new event.

        This method will read new connection event and store the connection
        (socket) into the connections attribute on the controller.

        It also clear all references to the connection since it is a new
        connection on the same ip:port.

        Parameters:
            event (KycoEvent): The received event (kytos/core.connection.new)
            with the needed infos.
        """
        log.info("Handling KycoEvent:kytos/core.connection.new ...")

        connection = event.source

        # Remove old connection (aka cleanup) if exists
        if self.get_connection_by_id(connection.id):
            self.remove_connection(connection.id)

        # Update connections with the new connection
        self.create_or_update_connection(connection)

    def add_new_switch(self, switch):
        """Add a new switch on the controller.

        Parameters:
            switch (Switch): A Switch object
        """
        self.switches[switch.dpid] = switch

    def load_napp(self, napp_name):
        """Load a single app.

        Load a single NAPP based on its name.

        Parameters:
            napp_name (str): Name of the NApp to be loaded.
        """
        path = os.path.join(self.options.napps, napp_name, 'main.py')
        module = SourceFileLoader(napp_name, path)

        napp = module.load_module().Main(controller=self)
        self.napps[napp_name] = napp

        for event_type, listeners in napp._listeners.items():
            if event_type not in self.events_listeners:
                self.events_listeners[event_type] = []
            self.events_listeners[event_type].extend(listeners)

        napp.start()

    def install_napp(self, napp_name):
        """Install the requested NApp by its name.

        Downloads the NApps from the NApp network and install it.
        TODO: Download or git-clone?

        Parameters:
            napp_name (str): Name of the NApp to be installed.
        """
        pass

    def load_napps(self):
        """Load all NApps installed on the NApps dir."""
        napps_dir = self.options.napps
        try:
            for author in os.listdir(napps_dir):
                author_dir = os.path.join(napps_dir, author)
                for napp_name in os.listdir(author_dir):
                    full_name = "{}/{}".format(author, napp_name)
                    log.info("Loading app %s", full_name)
                    self.load_napp(full_name)
        except FileNotFoundError as e:
            log.error("Could not load napps: %s", e)

    def unload_napp(self, napp_name):
        """Unload a specific NApp based on its name.

        Parameters:
            napp_name (str): Name of the NApp to be unloaded.
        """
        napp = self.napps.pop(napp_name)
        napp.shutdown()
        # Removing listeners from that napp
        for event_type, listeners in napp._listeners.items():
            for listener in listeners:
                self.events_listeners[event_type].remove(listener)
            if len(self.events_listeners[event_type]) == 0:
                self.events_listeners.pop(event_type)

    def unload_napps(self):
        """Unload all loaded NApps that are not core NApps."""
        # list() is used here to avoid the error:
        # 'RuntimeError: dictionary changed size during iteration'
        # This is caused by looping over an dictionary while removing
        # items from it.
        for napp_name in list(self.napps):
            if not isinstance(self.napps[napp_name], KycoCoreNApp):
                self.unload_napp(napp_name)
