"""History module for VCI."""

from typing import Dict, Optional, Union
from datetime import datetime
import pandas as pd
from vnai import optimize_execution
from vnstock.core.base.registry import ProviderRegistry
from vnstock.core.types import DataCategory, ProviderType, TimeFrame
from vnstock.core.utils.interval import normalize_interval
from .const import (
    _TRADING_URL, _CHART_URL, _INTERVAL_MAP, 
    _OHLC_MAP, _RESAMPLE_MAP, _OHLC_DTYPE, _INTRADAY_URL, 
    _INTRADAY_MAP, _INTRADAY_DTYPE, _PRICE_DEPTH_MAP, _INDEX_MAPPING
)
from vnstock.core.models import TickerModel
from vnstock.core.utils.logger import get_logger
from vnstock.core.utils.market import trading_hours
from vnstock.core.utils.parser import get_asset_type
from vnstock.core.utils.validation import validate_symbol
from vnstock.core.utils.user_agent import get_headers
from vnstock.core.utils.client import send_request, ProxyConfig
from vnstock.core.utils.transform import ohlc_to_df, intraday_to_df

logger = get_logger(__name__)


@ProviderRegistry.register(DataCategory.QUOTE, "vci", ProviderType.SCRAPING)
class Quote:
    """
    The Quote class is used to fetch historical price data from VCI.

    Parameters:
        - symbol (required): the stock symbol to fetch data for.
        - random_agent (optional): whether to use random user agent. Default is False.
        - proxy_config (optional): proxy configuration. Default is None.
        - show_log (optional): whether to show log. Default is True.
    """
    def __init__(self, symbol, random_agent=False, proxy_config: Optional[ProxyConfig]=None, show_log=True):
        self.symbol = validate_symbol(symbol)
        self.data_source = 'VCI'
        self._history = None  # Cache for historical data
        self.asset_type = get_asset_type(self.symbol)
        self.base_url = _TRADING_URL
        self.headers = get_headers(data_source=self.data_source, random_agent=random_agent)
        self.interval_map = _INTERVAL_MAP
        self.show_log = show_log
        self.proxy_config = proxy_config if proxy_config is not None else ProxyConfig()

        if not show_log:
            logger.setLevel('CRITICAL')

        if 'INDEX' in self.symbol:
            self.symbol = self._index_validation()

    def _index_validation(self) -> str:
        """
        If symbol contains 'INDEX' substring, validate it with _INDEX_MAPPING.
        """
        if self.symbol not in _INDEX_MAPPING.keys():
            raise ValueError(f"Không tìm thấy mã chứng khoán {self.symbol}. Các giá trị hợp lệ: {', '.join(_INDEX_MAPPING.keys())}")
        return _INDEX_MAPPING[self.symbol]

    def _input_validation(self, start: str, end: str, interval: str):
        """
        Validate input data
        """
        timeframe = normalize_interval(interval)
        ticker = TickerModel(
            symbol=self.symbol, start=start, end=end, 
            interval=str(timeframe)
        )

        if timeframe not in self.interval_map.values():
            msg = (
                f"Giá trị interval không hợp lệ: {timeframe}. "
                f"Vui lòng chọn: 1m, 5m, 15m, 30m, 1H, 1D, 1W, 1M"
            )
            raise ValueError(msg)

        return ticker

    @optimize_execution("VCI")
    def history(self, start: str, end: Optional[str]=None, interval: Optional[str]="1D", 
                show_log: Optional[bool]=False, 
                count_back: Optional[int]=None, floating: Optional[int]=2) -> pd.DataFrame:
        """
        Tải lịch sử giá của mã chứng khoán từ nguồn dữ liệu VCI.

        Tham số:
            - start (bắt buộc): thời gian bắt đầu lấy dữ liệu, có thể là ngày dạng string kiểu "YYYY-MM-DD" hoặc "YYYY-MM-DD HH:MM:SS".
            - end (tùy chọn): thời gian kết thúc lấy dữ liệu. Mặc định là None, chương trình tự động lấy thời điểm hiện tại.
            - interval (tùy chọn): Khung thời gian trích xuất dữ liệu giá lịch sử. Giá trị nhận: 1m, 5m, 15m, 30m, 1H, 1D, 1W, 1M. Mặc định là "1D".
            - show_log (tùy chọn): Hiển thị thông tin log giúp debug dễ dàng. Mặc định là False.
            - count_back (tùy chọn): Số lượng dữ liệu trả về từ thời điểm cuối.
            - floating (tùy chọn): Số chữ số thập phân cho giá. Mặc định là 2.
        """
        # Validate inputs
        ticker = self._input_validation(start, end, interval)

        # Hỗ trợ cả định dạng ngày và định dạng ngày giờ
        try:
            # Thử với định dạng ngày giờ
            start_time = datetime.strptime(ticker.start, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Thử với định dạng ngày
                start_time = datetime.strptime(ticker.start, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"Định dạng ngày không hợp lệ: {ticker.start}. Sử dụng định dạng YYYY-MM-DD hoặc YYYY-MM-DD HH:MM:SS")
        
        # Calculate end timestamp
        if end is not None:
            try:
                # Thử với định dạng ngày giờ
                end_time = datetime.strptime(ticker.end, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    # Thử với định dạng ngày
                    end_time = datetime.strptime(ticker.end, "%Y-%m-%d") + pd.Timedelta(days=1)
                except ValueError:
                    raise ValueError(f"Định dạng ngày không hợp lệ: {ticker.end}. Sử dụng định dạng YYYY-MM-DD hoặc YYYY-MM-DD HH:MM:SS")
            
            if start_time > end_time:
                raise ValueError("Thời gian bắt đầu không thể lớn hơn thời gian kết thúc.")
            end_stamp = int(end_time.timestamp())
        else:
            end_time = datetime.now() + pd.Timedelta(days=1)
            end_stamp = int(end_time.timestamp())

        interval_value = self.interval_map[str(ticker.interval)]

        # Tự động tính count_back nếu không truyền vào
        auto_count_back = 1000
        business_days = pd.bdate_range(start=start_time, end=end_time)

        if count_back is None and end is not None:
            # Lấy giá trị interval đã mapping
            interval_mapped = interval_value
            
            if interval_mapped == "ONE_DAY":
                # Sử dụng bdate_range để đếm số ngày làm việc (không tính thứ 7, chủ nhật)
                auto_count_back = len(business_days) + 1
            elif interval_mapped == "ONE_HOUR":
                # Tính số ngày làm việc và nhân với số giờ giao dịch mỗi ngày (6.5 giờ)
                auto_count_back = len(business_days) * 6.5 + 1
            elif interval_mapped == "ONE_MINUTE":
                # Tính số ngày làm việc và nhân với số phút giao dịch mỗi ngày (6.5 * 60 phút)
                auto_count_back = len(business_days) * 6.5 * 60 + 1
        else:
            auto_count_back = count_back if count_back is not None else 1000

        # Prepare request
        url = f'{self.base_url}chart/OHLCChart/gap-chart'
        payload = {
            "timeFrame": interval_value,
            "symbols": [self.symbol],
            "to": end_stamp,
            "countBack": auto_count_back
        }

        # Use the send_request utility from api_client
        json_data = send_request(
            url=url, 
            headers=self.headers, 
            method="POST", 
            payload=payload, 
            show_log=show_log,
            proxy_list=self.proxy_config.proxy_list,
            proxy_mode=self.proxy_config.proxy_mode,
            request_mode=self.proxy_config.request_mode,
            hf_proxy_url=self.proxy_config.hf_proxy_url
        )

        if not json_data:
            raise ValueError("Không tìm thấy dữ liệu. Vui lòng kiểm tra lại mã chứng khoán hoặc thời gian truy xuất.")
        
        # Use the ohlc_to_df utility from data_transform
        df = pd.DataFrame(dt)
        df['day'] = df['day'].astype('datetime64')

        return df

    @optimize_execution("VCI")
    def intraday(self, page_size: Optional[int]=100, last_time: Optional[str]=None,
                show_log: bool=False) -> pd.DataFrame:
        """
        Truy xuất dữ liệu khớp lệnh của mã chứng khoán bất kỳ từ nguồn dữ liệu VCI

        Tham số:
            - page_size (tùy chọn): Số lượng dữ liệu trả về trong một lần request. Mặc định là 100.
            - last_time (tùy chọn): Thời gian cắt dữ liệu, dùng để lấy dữ liệu sau thời gian cắt. Mặc định là None.
            - show_log (tùy chọn): Hiển thị thông tin log giúp debug dễ dàng. Mặc định là False.
        """
        market_status = trading_hours(None)
        if market_status['is_trading_hour'] is False and market_status['data_status'] == 'preparing':
            raise ValueError(f"{market_status['time']}: Dữ liệu khớp lệnh không thể truy cập trong thời gian chuẩn bị phiên mới. Vui lòng quay lại sau.")

        if self.symbol is None:
            raise ValueError("Vui lòng nhập mã chứng khoán cần truy xuất khi khởi tạo Trading Class.")

        if page_size > 30_000:
            logger.warning("Bạn đang yêu cầu truy xuất quá nhiều dữ liệu, điều này có thể gây lỗi quá tải.")

        url = f'{self.base_url}{_INTRADAY_URL}/LEData/getAll'
        payload = {
            "symbol": self.symbol,
            "limit": page_size,
            "truncTime": last_time
        }

        # Fetch data using the send_request utility
        data = send_request(
            url=url, 
            headers=self.headers, 
            method="POST", 
            payload=payload, 
            show_log=show_log,
            proxy_list=self.proxy_config.proxy_list,
            proxy_mode=self.proxy_config.proxy_mode,
            request_mode=self.proxy_config.request_mode,
            hf_proxy_url=self.proxy_config.hf_proxy_url
        )

        # Transform data using intraday_to_df utility
        df = intraday_to_df(
            data=data, 
            column_map=_INTRADAY_MAP, 
            dtype_map=_INTRADAY_DTYPE, 
            symbol=self.symbol, 
            asset_type=self.asset_type, 
            source=self.data_source
        )

        return df

    @optimize_execution("VCI")
    def price_depth(self, show_log: Optional[bool]=False) -> pd.DataFrame:
        """
        Truy xuất thống kê độ bước giá & khối lượng khớp lệnh của mã chứng khoán bất kỳ từ nguồn dữ liệu VCI.

        Tham số:
            - show_log (tùy chọn): Hiển thị thông tin log giúp debug dễ dàng. Mặc định là False.
        """
        market_status = trading_hours(None)
        if market_status['is_trading_hour'] is False and market_status['data_status'] == 'preparing':
            raise ValueError(f"{market_status['time']}: Dữ liệu khớp lệnh không thể truy cập trong thời gian chuẩn bị phiên mới. Vui lòng quay lại sau.")

        if self.symbol is None:
            raise ValueError("Vui lòng nhập mã chứng khoán cần truy xuất khi khởi tạo Trading Class.")

        url = f'{self.base_url}{_INTRADAY_URL}/AccumulatedPriceStepVol/getSymbolData'
        payload = {
            "symbol": self.symbol
        }

        # Fetch data using the send_request utility
        data = send_request(
            url=url, 
            headers=self.headers, 
            method="POST", 
            payload=payload, 
            show_log=show_log,
            proxy_list=self.proxy_config.proxy_list,
            proxy_mode=self.proxy_config.proxy_mode,
            request_mode=self.proxy_config.request_mode,
            hf_proxy_url=self.proxy_config.hf_proxy_url
        )

        # Process the data to DataFrame
        df = pd.DataFrame(data)
        
        # Select columns in _PRICE_DEPTH_MAP values and rename them
        df = df[_PRICE_DEPTH_MAP.keys()]
        df.rename(columns=_PRICE_DEPTH_MAP, inplace=True)
        
        df.source = self.data_source

        return df
