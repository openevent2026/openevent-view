import unittest
from collections import namedtuple
from concurrent import futures
from time import sleep

import grpc

from openevent.view.rpc import RpcTimeoutInterceptor


CallDetails = namedtuple(
    "CallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
)


class RpcTests(unittest.TestCase):
    def test_adds_default_timeout(self):
        interceptor = RpcTimeoutInterceptor(10.0)
        captured = []

        interceptor.intercept_unary_unary(
            lambda details, request: captured.append((details, request)),
            CallDetails("/service/method", None, None, None, None, None),
            "request",
        )

        self.assertEqual(captured[0][0].timeout, 10.0)
        self.assertEqual(captured[0][1], "request")

    def test_preserves_shorter_timeout(self):
        interceptor = RpcTimeoutInterceptor(10.0)
        captured = []

        interceptor.intercept_unary_unary(
            lambda details, request: captured.append(details),
            CallDetails("/service/method", 3.0, None, None, None, None),
            "request",
        )

        self.assertEqual(captured[0].timeout, 3.0)

    def test_intercepted_channel_enforces_deadline(self):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        handler = grpc.unary_unary_rpc_method_handler(
            lambda request, context: (sleep(0.2), b"response")[1],
            request_deserializer=lambda value: value,
            response_serializer=lambda value: value,
        )
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler("test.Service", {"Call": handler}),)
        )
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        base_channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        channel = grpc.intercept_channel(base_channel, RpcTimeoutInterceptor(0.05))
        call = channel.unary_unary(
            "/test.Service/Call",
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            with self.assertRaises(grpc.RpcError) as raised:
                call(b"request")
            self.assertEqual(raised.exception.code(), grpc.StatusCode.DEADLINE_EXCEEDED)
        finally:
            channel.close()
            server.stop(0).wait()


if __name__ == "__main__":
    unittest.main()
