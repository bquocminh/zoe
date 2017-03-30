"""Test script for unsuccessful cookie authentication."""

import sys
import time
import unittest

import requests


class ZoeRestTestSuccess(unittest.TestCase):
    """Test case class."""

    uri = 'http://localhost:5001/api/0.7/'
    id = '-1'
    session = None

    def tearDown(self):
        """Test end."""
        time.sleep(3)

    def test_0_login_fail(self):
        """Test failed login api endpoint."""
        print('Test failed login api endpoint')
        session = requests.Session()
        req = session.get(self.__class__.uri + 'login', auth=('test', '123'))

        self.assertEqual(req.status_code, 401)

    def test_1_login(self):
        """Test login api endpoint."""
        print('Test login api endpoint')

        session = requests.Session()
        req = session.get(self.__class__.uri + 'login', auth=('test', '1234'))

        self.assertEqual(req.status_code, 200)

        self.assertGreater(len(session.cookies.items()), 0)

        self.__class__.session = session

    def test_4_execution_details(self):
        """Test execution details api endpoint."""
        print('Test execution details api endpoint')
        session = self.__class__.session
        req = session.get(self.__class__.uri + 'execution/' + self.__class__.id)
        self.assertEqual(req.status_code, 404)

    def test_5_terminate_execution(self):
        """Test terminate execution api endpoint."""
        print('Test terminate execution api endpoint')
        session = self.__class__.session
        req = session.delete(self.__class__.uri + 'execution/' + self.__class__.id)
        self.assertEqual(req.status_code, 404)

    def test_7_delete_execution(self):
        """Test delete execution api endpoint."""
        print('Test delete execution api endpoint')
        session = self.__class__.session
        req = session.delete(self.__class__.uri + 'execution/delete/' + self.__class__.id)
        self.assertEqual(req.status_code, 404)

    def test_3_start_execution(self):
        """Test start execution api endpoint."""
        print('Test start execution api endpoint')

        session = self.__class__.session

        req = session.post(self.__class__.uri + 'execution', json={"application": "data", "name": "requests"})
        self.assertEqual(req.status_code, 400)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        API_SERVER = sys.argv.pop()
        ZoeRestTestSuccess.uri = 'http://' + API_SERVER + '/api/0.7/'

    unittest.main()