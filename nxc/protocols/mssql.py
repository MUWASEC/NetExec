import os
import random
import contextlib

from nxc.config import process_secret
from nxc.connection import connection
from nxc.connection import requires_admin
from nxc.logger import NXCAdapter
from nxc.helpers.bloodhound import add_user_bh
from nxc.helpers.powershell import create_ps_command
from nxc.protocols.mssql.mssqlexec import MSSQLEXEC
from nxc.protocols.mssql.mssql_ntlm_parser import parse_challenge

from impacket import tds, ntlm
from impacket.krb5.ccache import CCache
from impacket.tds import (
    SQLErrorException,
    TDS_LOGINACK_TOKEN,
    TDS_ERROR_TOKEN,
    TDS_ENVCHANGE_TOKEN,
    TDS_INFO_TOKEN,
    TDS_ENVCHANGE_VARCHAR,
    TDS_ENVCHANGE_DATABASE,
    TDS_ENVCHANGE_LANGUAGE,
    TDS_ENVCHANGE_CHARSET,
    TDS_ENVCHANGE_PACKETSIZE,
)

class mssql(connection):
    def __init__(self, args, db, host):
        self.mssql_instances = []
        self.domain = None
        self.server_os = None
        self.hash = None
        self.os_arch = None
        self.nthash = ""

        connection.__init__(self, args, db, host)

    def proto_logger(self):
        self.logger = NXCAdapter(
            extra={
                "protocol": "MSSQL",
                "host": self.host,
                "port": self.port,
                "hostname": "None",
            }
        )

    def enum_host_info(self):
        challenge = None
        try:
            login = tds.TDS_LOGIN()
            login["HostName"] = ""
            login["AppName"] = ""
            login["ServerName"] = self.conn.server.encode("utf-16le")
            login["CltIntName"] = login["AppName"]
            login["ClientPID"] = random.randint(0, 1024)
            login["PacketSize"] = self.conn.packetSize
            login["OptionFlags2"] = tds.TDS_INIT_LANG_FATAL | tds.TDS_ODBC_ON | tds.TDS_INTEGRATED_SECURITY_ON
            
            # NTLMSSP Negotiate
            auth = ntlm.getNTLMSSPType1("", "")
            login["SSPI"] = auth.getData()
            login["Length"] = len(login.getData())

            # Get number of mssql instance
            self.mssql_instances = self.conn.getInstances(0)

            # Send the NTLMSSP Negotiate or SQL Auth Packet
            self.conn.sendTDS(tds.TDS_LOGIN7, login.getData())

            tdsx = self.conn.recvTDS()
            challenge = tdsx["Data"][3:]
            self.logger.info(f"NTLM challenge: {challenge!s}")
        except Exception as e:
            self.logger.info(f"Failed to receive NTLM challenge, reason: {e!s}")
        else:
            ntlm_info = parse_challenge(challenge)
            self.domain = ntlm_info["target_info"]["MsvAvDnsDomainName"]
            self.hostname = ntlm_info["target_info"]["MsvAvNbComputerName"]
            self.server_os = f'Windows NT {ntlm_info["version"]}'
            self.logger.extra["hostname"] = self.hostname

        self.db.add_host(
            self.host,
            self.hostname,
            self.domain,
            self.server_os,
            len(self.mssql_instances),
        )

        with contextlib.suppress(Exception):
            self.conn.disconnect()

    def print_host_info(self):
        self.logger.display(f"{self.server_os} (name:{self.hostname}) (domain:{self.domain})")
        return True

    def create_conn_obj(self):
        try:
            self.conn = tds.MSSQL(self.host, self.port)
            self.conn.connect()
        except OSError as e:
            self.logger.debug(f"Error connecting to MSSQL: {e}")
            return False
        return True

    def check_if_admin(self):
        try:
            results = self.conn.sql_query("SELECT IS_SRVROLEMEMBER('sysadmin')")
            is_admin = int(results[0][""])
        except Exception as e:
            self.logger.fail(f"Error querying for sysadmin role: {e}")
            return False

        if is_admin:
            self.admin_privs = True
            self.logger.debug("User is admin")
        else:
            return False
        return True

    def kerberos_login(
        self,
        domain,
        username,
        password="",
        ntlm_hash="",
        aesKey="",
        kdcHost="",
        useCache=False,
    ):
        with contextlib.suppress(Exception):
            self.conn.disconnect()
        self.create_conn_obj()

        hashes = None
        if ntlm_hash != "":
            if ntlm_hash.find(":") != -1:
                hashes = ntlm_hash
                ntlm_hash.split(":")[1]
            else:
                # only nt hash
                hashes = f":{ntlm_hash}"

        kerb_pass = next(s for s in [self.nthash, password, aesKey] if s) if not all(s == "" for s in [self.nthash, password, aesKey]) else ""
        used_ccache = " from ccache" if useCache else f":{process_secret(kerb_pass)}"
        try:
            res = self.conn.kerberosLogin(
                None,
                username,
                password,
                domain,
                hashes,
                aesKey,
                kdcHost=kdcHost,
                useCache=useCache,
            )
            if res is not True:
                error_msg = self.conn.printReplies()
                self.logger.fail(
                    "{}\\{}:{} {}".format(
                        domain,
                        username,
                        used_ccache,
                        error_msg if error_msg else ""
                    )
                )
                return False
        except BrokenPipeError:
            self.logger.fail("Broken Pipe Error while attempting to login")
            return False
        except Exception as e:
            domain = f"{domain}\\" if not self.args.local_auth else ""
            self.logger.fail(f"{domain}{username}{used_ccache} ({e!s})")
            return False
        else:
            self.password = password
            if username == "" and useCache:
                ccache = CCache.loadFile(os.getenv("KRB5CCNAME"))
                principal = ccache.principal.toPrincipal()
                self.username = principal.components[0]
                username = principal.components[0]
            else:
                self.username = username
            self.domain = domain
            self.check_if_admin()

            domain = f"{domain}\\" if not self.args.local_auth else ""

            self.logger.success(f"{domain}{username}{used_ccache} {self.mark_pwned()}")
            if not self.args.local_auth:
                add_user_bh(self.username, self.domain, self.logger, self.config)
            if self.admin_privs:
                add_user_bh(f"{self.hostname}$", domain, self.logger, self.config)
            return True

    def plaintext_login(self, domain, username, password):
        with contextlib.suppress(Exception):
            self.conn.disconnect()
        self.create_conn_obj()

        try:
            # domain = "" is to prevent a decoding issue in impacket/ntlm.py:617 where it attempts to decode the domain
            res = self.conn.login(None, username, password, domain if domain else "", None, not self.args.local_auth)
            if res is not True:
                error_msg = self.handle_mssql_reply()
                self.logger.fail(
                    "{}\\{}:{} {}".format(
                        domain,
                        username,
                        process_secret(password),
                        error_msg if error_msg else ""
                    )
                )
                return False
        except BrokenPipeError:
            self.logger.fail("Broken Pipe Error while attempting to login")
            return False
        except Exception as e:
            self.logger.fail(f"{domain}\\{username}:{process_secret(password)} ({e!s})")
            return False
        else:
            self.password = password
            self.username = username
            self.domain = domain
            self.check_if_admin()
            self.db.add_credential("plaintext", domain, username, password)

            if self.admin_privs:
                self.db.add_admin_user("plaintext", domain, username, password, self.host)
                add_user_bh(f"{self.hostname}$", domain, self.logger, self.config)

            domain = f"{domain}\\" if not self.args.local_auth else ""
            out = f"{domain}{username}:{process_secret(password)} {self.mark_pwned()}"
            self.logger.success(out)
            if not self.args.local_auth:
                add_user_bh(self.username, self.domain, self.logger, self.config)
            return True

    def hash_login(self, domain, username, ntlm_hash):
        with contextlib.suppress(Exception):
            self.conn.disconnect()
        self.create_conn_obj()

        lmhash = ""
        nthash = ""

        # This checks to see if we didn't provide the LM Hash
        if ntlm_hash.find(":") != -1:
            lmhash, nthash = ntlm_hash.split(":")
        else:
            nthash = ntlm_hash

        try:
            res = self.conn.login(
                None,
                username,
                "",
                domain,
                ":" + nthash if not lmhash else ntlm_hash,
                not self.args.local_auth,
            )
            if res is not True:
                error_msg = self.conn.printReplies()
                self.logger.fail(
                    "{}\\{}:{} {}".format(
                        domain,
                        username,
                        process_secret(nthash),
                        error_msg if error_msg else ""
                    )
                )
                return False
        except BrokenPipeError:
            self.logger.fail("Broken Pipe Error while attempting to login")
            return False
        except Exception as e:
            self.logger.fail(f"{domain}\\{username}:{process_secret(ntlm_hash)} ({e!s})")
            return False
        else:
            self.hash = ntlm_hash
            self.username = username
            self.domain = domain
            self.check_if_admin()
            self.db.add_credential("hash", domain, username, ntlm_hash)

            if self.admin_privs:
                self.db.add_admin_user("hash", domain, username, ntlm_hash, self.host)
                add_user_bh(f"{self.hostname}$", domain, self.logger, self.config)

            out = f"{domain}\\{username} {process_secret(ntlm_hash)} {self.mark_pwned()}"
            self.logger.success(out)
            if not self.args.local_auth:
                add_user_bh(self.username, self.domain, self.logger, self.config)
            return True

    def mssql_query(self):
        if self.conn.lastError:
            # Invalid connection
            return None
        query = self.args.mssql_query
        self.logger.info(f"Query to run:\n{query}")
        try:
            raw_output = self.conn.sql_query(query)
            self.logger.info("Executed MSSQL query")
            self.logger.debug(f"Raw output: {raw_output}")
            for data in raw_output:
                if isinstance(data, dict):
                    for key, value in data.items():
                        if key:
                            self.logger.highlight(f"{key}:{value}")
                        else:
                            self.logger.highlight(f"{value}")
                else:
                    self.logger.fail("Unexpected output")
        except Exception as e:
            self.logger.exception(e)
            return None

        return raw_output

    @requires_admin
    def execute(self, payload=None, print_output=False):
        if not payload and self.args.execute:
            payload = self.args.execute

        self.logger.info(f"Command to execute:\n{payload}")
        try:
            exec_method = MSSQLEXEC(self.conn)
            raw_output = exec_method.execute(payload, print_output)
            self.logger.info("Executed command via mssqlexec")
            self.logger.debug(f"Raw output: {raw_output}")
        except Exception as e:
            self.logger.exception(e)
            return None

        if hasattr(self, "server"):
            self.server.track_host(self.host)

        if self.args.execute or self.args.ps_execute:
            self.logger.success("Executed command via mssqlexec")
            if self.args.no_output:
                self.logger.debug("Output set to disabled")
            else:
                for line in raw_output:
                    self.logger.highlight(line)

        return raw_output

    @requires_admin
    def ps_execute(
        self,
        payload=None,
        get_output=False,
        methods=None,
        force_ps32=False,
        dont_obfs=True,
    ):
        if not payload and self.args.ps_execute:
            payload = self.args.ps_execute
            if not self.args.no_output:
                get_output = True

        # We're disabling PS obfuscation by default as it breaks the MSSQLEXEC execution method
        ps_command = create_ps_command(payload, force_ps32=force_ps32, dont_obfs=dont_obfs)
        return self.execute(ps_command, get_output)

    @requires_admin
    def put_file(self):
        self.logger.display(f"Copy {self.args.put_file[0]} to {self.args.put_file[1]}")
        with open(self.args.put_file[0], "rb") as f:
            try:
                data = f.read()
                self.logger.display(f"Size is {len(data)} bytes")
                exec_method = MSSQLEXEC(self.conn)
                exec_method.put_file(data, self.args.put_file[1])
                if exec_method.file_exists(self.args.put_file[1]):
                    self.logger.success("File has been uploaded on the remote machine")
                else:
                    self.logger.fail("File does not exist on the remote system... error during upload")
            except Exception as e:
                self.logger.fail(f"Error during upload : {e}")

    @requires_admin
    def get_file(self):
        remote_path = self.args.get_file[0]
        download_path = self.args.get_file[1]
        self.logger.display(f'Copying "{remote_path}" to "{download_path}"')

        try:
            exec_method = MSSQLEXEC(self.conn)
            exec_method.get_file(self.args.get_file[0], self.args.get_file[1])
            self.logger.success(f'File "{remote_path}" was downloaded to "{download_path}"')
        except Exception as e:
            self.logger.fail(f'Error reading file "{remote_path}": {e}')
            if os.path.getsize(download_path) == 0:
                os.remove(download_path)

    # We hook these functions in the tds library to use nxc's logger instead of printing the output to stdout
    # The whole tds library in impacket needs a good overhaul to preserve my sanity
    def handle_mssql_reply(self):
        for keys in self.conn.replies:
            for _i, key in enumerate(self.conn.replies[keys]):
                if key["TokenType"] == TDS_ERROR_TOKEN:
                    error_msg = f"{key['MsgText'].decode('utf-16le')} Please try again with or without '--local-auth'"
                    self.conn.lastError = SQLErrorException(f"ERROR: Line {key['LineNumber']:d}: {key['MsgText'].decode('utf-16le')}")
                    return error_msg
                elif key["TokenType"] == TDS_INFO_TOKEN:
                    return f"{key['MsgText'].decode('utf-16le')}"
                elif key["TokenType"] == TDS_LOGINACK_TOKEN:
                    return f"ACK: Result: {key['Interface']} - {key['ProgName'].decode('utf-16le')} ({key['MajorVer']:d}{key['MinorVer']:d} {key['BuildNumHi']:d}{key['BuildNumLow']:d}) "
                elif key["TokenType"] == TDS_ENVCHANGE_TOKEN and key["Type"] in (
                    TDS_ENVCHANGE_DATABASE,
                    TDS_ENVCHANGE_LANGUAGE,
                    TDS_ENVCHANGE_CHARSET,
                    TDS_ENVCHANGE_PACKETSIZE,
                ):
                    record = TDS_ENVCHANGE_VARCHAR(key["Data"])
                    if record["OldValue"] == "":
                        record["OldValue"] = "None".encode("utf-16le")
                    elif record["NewValue"] == "":
                        record["NewValue"] = "None".encode("utf-16le")
                    if key["Type"] == TDS_ENVCHANGE_DATABASE:
                        _type = "DATABASE"
                    elif key["Type"] == TDS_ENVCHANGE_LANGUAGE:
                        _type = "LANGUAGE"
                    elif key["Type"] == TDS_ENVCHANGE_CHARSET:
                        _type = "CHARSET"
                    elif key["Type"] == TDS_ENVCHANGE_PACKETSIZE:
                        _type = "PACKETSIZE"
                    else:
                        _type = f"{key['Type']:d}"
                    return f"ENVCHANGE({_type}): Old Value: {record['OldValue'].decode('utf-16le')}, New Value: {record['NewValue'].decode('utf-16le')}"
