#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_server_manager.py - Manager for Data Server Subprocess

Manages the data server as a separate process that starts automatically
with the main API server and handles data distribution.
"""

import subprocess
import logging
import time
import sys
import os
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("lpu5-data-server-manager")

class DataServerManager:
    """Manages the data server subprocess"""
    
    def __init__(self, data_server_port: int = 8001, data_server_host: str = "127.0.0.1"):
        self.data_server_port = data_server_port
        self.data_server_host = data_server_host
        self.process: Optional[subprocess.Popen] = None
        self.base_url = f"http://{data_server_host}:{data_server_port}"
        self._broadcast_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="broadcast")
        self._broadcast_session = requests.Session()
        
    def start(self, timeout: int = 10) -> bool:
        """
        Start the data server as a subprocess.
        
        Args:
            timeout: Maximum time to wait for server to become ready (seconds)
            
        Returns:
            True if server started successfully, False otherwise
        """
        if self.is_running():
            logger.warning("Data server is already running")
            return True
        
        try:
            # Get the path to data_server.py
            current_dir = os.path.dirname(os.path.abspath(__file__))
            data_server_path = os.path.join(current_dir, "data_server.py")
            
            if not os.path.exists(data_server_path):
                logger.error(f"data_server.py not found at: {data_server_path}")
                return False
            
            # Start the data server subprocess
            logger.info(f"Starting data server subprocess: {data_server_path}")
            
            # Use the same Python interpreter that's running this script
            python_executable = sys.executable
            
            self.process = subprocess.Popen(
                [python_executable, data_server_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            logger.info(f"Data server subprocess started with PID: {self.process.pid}")
            
            # Wait for server to become ready
            start_time = time.time()
            while time.time() - start_time < timeout:
                if self.health_check():
                    logger.info(f"Data server is ready at {self.base_url}")
                    return True
                time.sleep(0.5)
            
            logger.error(f"Data server failed to become ready within {timeout} seconds")
            self.stop()
            return False
            
        except Exception as e:
            logger.error(f"Failed to start data server: {e}")
            return False
    
    def stop(self) -> bool:
        """
        Stop the data server subprocess.
        
        Returns:
            True if server stopped successfully, False otherwise
        """
        if self.process is None:
            logger.warning("Data server process is not running")
            return True
        
        try:
            logger.info(f"Stopping data server (PID: {self.process.pid})...")
            
            # Shut down the broadcast thread pool
            self._broadcast_executor.shutdown(wait=False)
            self._broadcast_session.close()
            
            # Try graceful shutdown first
            self.process.terminate()
            
            # Wait up to 5 seconds for graceful shutdown
            try:
                self.process.wait(timeout=5)
                logger.info("Data server stopped gracefully")
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown fails
                logger.warning("Data server didn't stop gracefully, forcing kill...")
                self.process.kill()
                self.process.wait()
                logger.info("Data server force killed")
            
            # Close subprocess pipes to prevent
            # ProactorBasePipeTransport._call_connection_lost errors on Windows
            try:
                if self.process.stdout:
                    self.process.stdout.close()
            except Exception:
                pass
            try:
                if self.process.stderr:
                    self.process.stderr.close()
            except Exception:
                pass
            
            self.process = None
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop data server: {e}")
            return False
    
    def is_running(self) -> bool:
        """
        Check if the data server process is running.
        
        Returns:
            True if running, False otherwise
        """
        if self.process is None:
            return False
        
        # Check if process is still alive
        if self.process.poll() is not None:
            # Process has terminated
            self.process = None
            return False
        
        return True
    
    def health_check(self) -> bool:
        """
        Check if the data server is healthy and responding.
        
        Returns:
            True if healthy, False otherwise
        """
        try:
            response = requests.get(f"{self.base_url}/api/health", timeout=2)
            return response.status_code == 200
        except Exception:
            return False
    
    def get_status(self) -> Optional[dict]:
        """
        Get the status of the data server.
        
        Returns:
            Status dict if successful, None otherwise
        """
        try:
            response = requests.get(f"{self.base_url}/api/status", timeout=2)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.error(f"Failed to get data server status: {e}")
            return None
    
    def broadcast(self, channel: str, message_type: str, data: dict) -> bool:
        """
        Broadcast data to clients via the data server (non-blocking).
        
        Submits the broadcast to a background thread pool so it doesn't block
        the calling API request handler.
        
        Args:
            channel: Channel to broadcast on (e.g., 'markers', 'drawings')
            message_type: Type of message (e.g., 'marker_created', 'marker_updated')
            data: Data to broadcast
            
        Returns:
            True if broadcast was submitted, False otherwise
        """
        try:
            payload = {
                "channel": channel,
                "type": message_type,
                "data": data
            }
            self._broadcast_executor.submit(self._do_broadcast, payload)
            return True
        except Exception as e:
            logger.error(f"Failed to submit broadcast: {e}")
            return False

    def _do_broadcast(self, payload: dict) -> None:
        """Execute the actual HTTP broadcast in a background thread."""
        try:
            response = self._broadcast_session.post(
                f"{self.base_url}/api/broadcast",
                json=payload,
                timeout=5
            )
            if response.status_code == 200:
                logger.debug(f"Broadcast to channel '{payload.get('channel')}' successful")
            else:
                logger.warning(f"Broadcast failed with status {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to broadcast data: {e}")
    
    def restart(self, timeout: int = 10) -> bool:
        """
        Restart the data server.
        
        Args:
            timeout: Maximum time to wait for server to become ready (seconds)
            
        Returns:
            True if restart successful, False otherwise
        """
        logger.info("Restarting data server...")
        self.stop()
        time.sleep(1)
        return self.start(timeout=timeout)
