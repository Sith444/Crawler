# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 18:44
# @Desc    :

from httpx import RequestError


class DataFetchError(RequestError):
    """something error when fetch"""


class IPBlockError(RequestError):
    """fetch so fast that the server block us ip"""
