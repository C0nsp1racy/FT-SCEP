from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from binascii import unhexlify as uhx
from Crypto.Cipher import PKCS1_OAEP
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
from os.path import exists
from random import randint
from zlib import compress
from re import search, match
from json import load
import requests, socket, json
from time import sleep

def _apicall (drive, request, maximum_backoff=32):
    sleep_exponent_count = 0
    _error = None
    while True:
        success = True
        retry = False
        try:
            response = request.execute()
        except HttpError as error:
            print(error)
            _error = error
            success = False
            try:
                error_details = json.loads(error.content.decode("utf-8"))["error"]
            except json.decoder.JSONDecodeError as error:
                retry = True
            else:
                if "errors" in error_details:
                    if error_details["errors"][0]["reason"] in ("dailyLimitExceeded", "userRateLimitExceeded", "rateLimitExceeded", "backendError", "sharingRateLimitExceeded", "failedPrecondition", "internalError", "domainPolicy", "insufficientFilePermissions", "appNotAuthorizedToFile"): # IF REQUEST IS RETRYABLE
                        retry = True
                else:
                    raise error
        except (TransportError, socket.error, socket.timeout) as error:
            print(error)
            _error = error
            success = False
            retry = True
        if success:
            break
        if retry:
            sleep_time = 2^sleep_exponent_count
            if sleep_time < maximum_backoff:
                sleep(sleep_time)
                sleep_exponent_count += 1
                continue
            else:
                raise Exception("Maximum Backoff Limit Exceeded.")
        else:
            raise Exception("Unretryable Error")
    return response

def doEncrypt(key, buf):
    return AES.new(key, AES.MODE_ECB).encrypt(
        buf + (b"\x00" * (0x10 - (len(buf) % 0x10)))
    )


def encrypt(
    in_bytes, public_key, vm_file=None, *, drmkey="c9674744cfce53f3a3ee187a15869795"
):

    pubKey = RSA.importKey(open(public_key).read())
    aesKey = randint(0, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF).to_bytes(0x10, "big")
    buf = None
    inp = b""

    if vm_file:
        with open(vm_file, "rb") as f:
            tmp = f.read()
            inp += b"\x13\x37\xB0\x0B"
            inp += len(tmp).to_bytes(4, "little")
            inp += tmp

    if vm_file:
        inp += doEncrypt(uhx(drmkey[0:32]), in_bytes)
    else:
        inp += in_bytes

    compressed = compress(inp, 9)

    return (
        b"TINFOIL\xFE"
        + PKCS1_OAEP.new(pubKey, hashAlgo=SHA256, label=b"").encrypt(aesKey)
        + len(compressed).to_bytes(8, "little")
        + doEncrypt(aesKey, compressed)
    )

def generate_shop(minidb, domain):
    shop_files = []
    for i in minidb:
        if "mirrors" in i:
            for j in i["mirrors"].values():  # unlisted
                for k in j:
                    if k["id"] is not None:
                        shop_files.append(
                            {
                                "url": "{}{}#{}".format(domain, k["id"], k["filename"]),
                                "size": int(k["size"]),
                            }
                        )
    return shop_files


def get_creds(credentials, token, scopes=["https://www.googleapis.com/auth/drive"]):
    creds = None
    if exists(token):
        with open(token, "r") as t:
            creds = Credentials(**load(t))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(
                credentials, scopes
            ).run_local_server(port=0)
        with open(token, "w") as t:
            # https://github.com/googleapis/google-auth-library-python/issues/666
            # Avoids issues when loading the token again. 
            creds.expiry = None
            json_creds = creds.to_json()
            t.write(json_creds)
    return creds


def generate_entry(item):
    return {"id": item["id"], "filename": item["name"], "version": int(item["version"]), "size": int(item["size"])}


def find_title_id(name):
    tid = search(r"0[0-9A-Fa-f]{15}", name)
    if tid is not None:
        return tid.group(0).upper()
    return


def valid_file_id(file_id):
    return match(r"[-\w]{25,}$",file_id)


def lsf(service, parent):
    files = []
    resp = {"nextPageToken": None}
    while "nextPageToken" in resp:
        resp = (
            service.files()
            .list(
                q='trashed = false and "{}" in parents and not mimeType = "application/vnd.google-apps.folder"'.format(
                    parent
                ),
                fields="files(id,name,size,fileExtension),nextPageToken",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=resp["nextPageToken"],
            )
            .execute()
        )
        files += resp["files"]
    return files

def _ls(drive, folder_id, fields="files(id,name,size,fileExtension,permissionIds),nextPageToken", searchTerms=""):
    files = []
    resp = {"nextPageToken": None}
    while "nextPageToken" in resp:
        resp = _apicall(drive, drive.files().list(
            q = " and ".join(["\"%s\" in parents" % folder_id] + [searchTerms] + ["trashed = false"]),
            fields = fields,
            pageSize = 1000,
            supportsAllDrives = True,
            includeItemsFromAllDrives = True,
            pageToken = resp["nextPageToken"]
        ))
        files += resp["files"]
    return files
    
def _lsd(drive, folder_id):
    return _ls(drive,
        folder_id,
        searchTerms="mimeType contains \"application/vnd.google-apps.folder\""
    )

def _lsf(drive, folder_id, fields="files(id,name,size,fileExtension,permissionIds),nextPageToken"):
    return _ls(drive,
        folder_id,
        fields=fields,
        searchTerms="not mimeType contains \"application/vnd.google-apps.folder\""
    )

def check_file_shared(drive, file_to_check):
    if "permissionIds" in file_to_check:
        for permission in file_to_check["permissionIds"]:
            if "anyoneWithLink" == permission:
                return True
    return True


    
def get_all_files_in_folder(drive, folder_id, dict_files, recursion=True):
    for _file in _lsf(drive, folder_id):
        if "size" in _file:
            realver = search(r"\[v[0-9]{5,}\]", _file["name"]) #tinfoil assums v0 if missing
            if realver is None:
                dict_files.append({"id": _file["id"], "size": _file["size"], "name": "[{}].{}".format(find_title_id(_file["name"]), _file["fileExtension"]), "version": 0, "fileExtension": _file["fileExtension"], "shared": check_file_shared(drive, _file)})
            else:
                realver=realver.group(0)
                dict_files.append({"id": _file["id"], "size": _file["size"], "name": "[{}]{}.{}".format(find_title_id(_file["name"]), realver, _file["fileExtension"]), "version": int(realver[2:-1]), "fileExtension": _file["fileExtension"], "shared": check_file_shared(drive, _file)})
    if recursion:
        for _folder in _lsd(drive, folder_id):
            get_all_files_in_folder(drive, _folder["id"], dict_files, recursion=recursion)