"""Tests for key resolution functions."""

import unittest
from unittest.mock import Mock

from src.rate_limiter.keys import (
    composite_key, header_key, ip_key, method_key, path_key,
    rate_limit_key, user_id_key
)


class TestKeyResolution(unittest.TestCase):
    """Test suite for key resolution functions."""

    def test_ip_key_fastapi(self):
        """Test IP extraction from FastAPI request."""
        # Mock FastAPI request
        request = Mock()
        request.headers = {'X-Forwarded-For': '192.168.1.100, 10.0.0.1'}
        request.client = Mock()
        request.client.host = '127.0.0.1'

        result = ip_key(request)
        self.assertEqual(result, '192.168.1.100')

    def test_ip_key_fastapi_no_proxy(self):
        """Test IP extraction from FastAPI request without proxy."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'

        result = ip_key(request)
        self.assertEqual(result, '203.0.113.10')

    def test_ip_key_flask(self):
        """Test IP extraction from Flask request."""
        # Mock Flask request
        request = Mock()
        request.headers = {'X-Forwarded-For': '192.168.1.100'}
        request.remote_addr = '10.0.0.1'

        result = ip_key(request)
        self.assertEqual(result, '192.168.1.100')

    def test_ip_key_flask_no_proxy(self):
        """Test IP extraction from Flask request without proxy."""
        request = Mock()
        request.headers = {}
        request.remote_addr = '203.0.113.10'

        result = ip_key(request)
        self.assertEqual(result, '203.0.113.10')

    def test_ip_key_multiple_proxies(self):
        """Test IP extraction with multiple proxies in X-Forwarded-For."""
        request = Mock()
        request.headers = {'X-Forwarded-For': '192.168.1.100, 10.0.0.1, 172.16.0.1'}

        result = ip_key(request)
        self.assertEqual(result, '192.168.1.100')

    def test_ip_key_fallback(self):
        """Test IP extraction fallback when no headers available."""
        request = Mock()
        request.headers = {}

        result = ip_key(request)
        self.assertEqual(result, '127.0.0.1')

    def test_user_id_key_fastapi_authenticated(self):
        """Test user ID extraction from authenticated FastAPI request."""
        request = Mock()
        request.user = Mock()
        request.user.id = 12345

        result = user_id_key(request)
        self.assertEqual(result, '12345')

    def test_user_id_key_flask_login(self):
        """Test user ID extraction from Flask-Login request."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.id = 67890

        result = user_id_key(request)
        self.assertEqual(result, '67890')

    def test_user_id_key_jwt(self):
        """Test user ID extraction from JWT token."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'}

        result = user_id_key(request)
        self.assertTrue(result.startswith('jwt:'))

    def test_user_id_key_api_key(self):
        """Test user ID extraction from API key."""
        request = Mock()
        request.headers = {'X-API-Key': 'secret-api-key-123'}

        result = user_id_key(request)
        self.assertTrue(result.startswith('api_key:'))

    def test_user_id_key_fallback_to_ip(self):
        """Test user ID fallback to IP when no authentication."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'

        result = user_id_key(request)
        self.assertTrue(result.startswith('anonymous:'))
        self.assertIn('203.0.113.10', result)

    def test_path_key_fastapi(self):
        """Test path extraction from FastAPI request."""
        request = Mock()
        request.path = '/api/v1/users'

        result = path_key(request)
        self.assertEqual(result, '/api/v1/users')

    def test_path_key_flask(self):
        """Test path extraction from Flask request."""
        request = Mock()
        request.url = 'http://example.com/api/v1/users?param=value'
        request.path = None  # Ensure path attribute is not present

        result = path_key(request)
        self.assertEqual(result, '/api/v1/users?param=value')

    def test_method_key_fastapi(self):
        """Test method extraction from FastAPI request."""
        request = Mock()
        request.method = 'POST'

        result = method_key(request)
        self.assertEqual(result, 'POST')

    def test_method_key_flask(self):
        """Test method extraction from Flask request."""
        request = Mock()
        request.request_method = 'GET'
        request.method = None  # Ensure method attribute is not present

        result = method_key(request)
        self.assertEqual(result, 'GET')

    def test_header_key(self):
        """Test header extraction key function."""
        tenant_key = header_key('X-Tenant-ID')

        request = Mock()
        request.headers = {'X-Tenant-ID': 'tenant-123'}

        result = tenant_key(request)
        self.assertEqual(result, 'tenant-123')

    def test_header_key_missing(self):
        """Test header extraction when header is missing."""
        version_key = header_key('X-API-Version')

        request = Mock()
        request.headers = {}

        result = version_key(request)
        self.assertEqual(result, 'no_X-API-Version')

    def test_header_key_case_insensitive(self):
        """Test header extraction with case insensitive matching."""
        version_key = header_key('X-API-Version')

        request = Mock()
        request.headers = {'x-api-version': 'v2'}

        result = version_key(request)
        self.assertEqual(result, 'v2')

    def test_composite_key(self):
        """Test composite key creation."""
        def simple_ip(request):
            return '192.168.1.100'

        def simple_user(request):
            return 'user123'

        composite = composite_key(simple_ip, simple_user)

        request = Mock()
        result = composite(request)

        self.assertEqual(result, '192.168.1.100:user123')

    def test_composite_key_with_error(self):
        """Test composite key when one function fails."""
        def working_func(request):
            return 'working'

        def failing_func(request):
            raise Exception('Failed')

        composite = composite_key(working_func, failing_func)

        request = Mock()
        result = composite(request)

        self.assertEqual(result, 'working:error')

    def test_rate_limit_key_ip_only(self):
        """Test rate limit key with IP only."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'

        key_func = rate_limit_key(ip=True)
        result = key_func(request)

        self.assertEqual(result, '203.0.113.10')

    def test_rate_limit_key_ip_and_path(self):
        """Test rate limit key with IP and path."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'
        request.path = '/api/users'

        key_func = rate_limit_key(ip=True, path=True)
        result = key_func(request)

        self.assertEqual(result, '203.0.113.10:/api/users')

    def test_rate_limit_key_user_and_method(self):
        """Test rate limit key with user and method (with IP as default)."""
        # Create proper headers mock that behaves like a dict
        class HeadersDict(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        request = Mock()
        request.user = Mock()
        request.user.id = 12345
        request.user.is_authenticated = True
        request.method = 'POST'
        request.client = Mock()
        request.client.host = '203.0.113.10'
        request.headers = HeadersDict({})  # Provide proper headers dict

        # ip=True is the default, so user=True + method=True includes IP
        key_func = rate_limit_key(user=True, method=True)
        result = key_func(request)

        # Result should be ip:user_id:method
        self.assertEqual(result, '203.0.113.10:12345:POST')

        # Test with ip=False to get only user and method
        key_func_no_ip = rate_limit_key(ip=False, user=True, method=True)
        result_no_ip = key_func_no_ip(request)
        self.assertEqual(result_no_ip, '12345:POST')

    def test_rate_limit_key_custom_functions(self):
        """Test rate limit key with custom functions."""
        def custom_func(request):
            return 'custom_value'

        request = Mock()
        request.client = Mock()
        request.client.host = '203.0.113.10'
        request.headers = {}  # Add headers to avoid ip_key error

        key_func = rate_limit_key(ip=True, custom_funcs=[custom_func])
        result = key_func(request)

        self.assertEqual(result, '203.0.113.10:custom_value')

    def test_rate_limit_key_all_options(self):
        """Test rate limit key with all options enabled."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'
        request.user = Mock()
        request.user.id = 12345
        request.path = '/api/users'
        request.method = 'POST'

        key_func = rate_limit_key(ip=True, user=True, path=True, method=True)
        result = key_func(request)

        expected = '203.0.113.10:12345:/api/users:POST'
        self.assertEqual(result, expected)

    def test_rate_limit_key_default_fallback(self):
        """Test rate limit key defaults to IP when no options specified."""
        request = Mock()
        request.headers = {}
        request.client = Mock()
        request.client.host = '203.0.113.10'

        key_func = rate_limit_key()  # No options specified
        result = key_func(request)

        self.assertEqual(result, '203.0.113.10')

    def test_real_world_scenarios(self):
        """Test key functions with realistic request scenarios."""
        # Create a proper headers mock that behaves like a dict
        class HeadersDict(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        # Scenario 1: API request with authentication
        api_request = Mock()
        api_request.headers = HeadersDict({
            'X-Forwarded-For': '203.0.113.10',
            'Authorization': 'Bearer token123',
            'X-Tenant-ID': 'acme-corp'
        })
        api_request.path = '/api/v1/data'
        api_request.method = 'POST'

        # Different key strategies
        # Note: ip=True is the default, so we need ip=False to test user-only
        ip_only = rate_limit_key(ip=True)(api_request)
        user_only = rate_limit_key(ip=False, user=True)(api_request)
        endpoint_specific = rate_limit_key(ip=True, path=True)(api_request)

        self.assertEqual(ip_only, '203.0.113.10')
        self.assertTrue(user_only.startswith('jwt:'))
        self.assertEqual(endpoint_specific, '203.0.113.10:/api/v1/data')

        # Scenario 2: Web request from authenticated user
        web_request = Mock()
        web_request.headers = HeadersDict({'X-Forwarded-For': '198.51.100.20'})
        web_request.user = Mock()
        web_request.user.is_authenticated = True
        web_request.user.id = 98765
        web_request.path = '/dashboard'
        web_request.method = 'GET'

        # ip=True is default, so we need ip=False to get only user and path
        user_session_key = rate_limit_key(ip=False, user=True, path=True)(web_request)
        self.assertEqual(user_session_key, '98765:/dashboard')


if __name__ == '__main__':
    unittest.main()
