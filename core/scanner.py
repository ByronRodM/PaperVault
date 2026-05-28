import gc
import os

WIA_FORMAT_BMP  = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_PNG  = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
WIA_DEVICE_TYPE_SCANNER = 1
WIA_IPA_DATATYPE = 4104  # 0=BW, 1=Gray, 2=Color
WIA_IPS_XRES     = 6146
WIA_IPS_YRES     = 6147


def _set_prop(properties, prop_id: int, value) -> bool:
    """Set a WIA property by ID. Returns False silently if unsupported."""
    try:
        for prop in properties:
            if prop.PropertyID == prop_id:
                prop.Value = value
                return True
    except Exception:
        pass
    return False


def listar_dispositivos() -> list:
    """Returns available WIA scanner devices."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    dm = None
    try:
        dm = win32com.client.Dispatch("WIA.DeviceManager")
        result = []
        for info in dm.DeviceInfos:
            if info.Type == WIA_DEVICE_TYPE_SCANNER:
                try:
                    nombre = info.Properties["Name"].Value
                except Exception:
                    nombre = info.DeviceID
                result.append({"id": info.DeviceID, "nombre": nombre})
        return result
    finally:
        dm = None
        gc.collect()
        pythoncom.CoUninitialize()


def escanear(device_id: str, dpi: int = 300, modo: str = "color", directorio: str = "uploads/tmp") -> str:
    """Scans a document and saves it as PNG. Returns the full saved path."""
    import pythoncom
    import win32com.client
    from PIL import Image

    pythoncom.CoInitialize()
    dm = device = item = wia_image = None
    try:
        dm = win32com.client.Dispatch("WIA.DeviceManager")
        for info in dm.DeviceInfos:
            if info.DeviceID == device_id:
                device = info.Connect()
                break
        if device is None:
            raise RuntimeError(f"Escáner no encontrado: {device_id}")

        item = device.Items[1]

        # Set properties one by one — ignore failures (driver may not support all)
        color_val = {"color": 2, "gris": 1, "bw": 0}.get(modo, 2)
        _set_prop(item.Properties, WIA_IPS_XRES, dpi)
        _set_prop(item.Properties, WIA_IPS_YRES, dpi)
        _set_prop(item.Properties, WIA_IPA_DATATYPE, color_val)

        # Try PNG → JPEG → BMP (BMP is universally supported by WIA drivers)
        tmp_path = None
        for fmt_guid, suffix in [
            (WIA_FORMAT_PNG,  ".png"),
            (WIA_FORMAT_JPEG, ".jpg"),
            (WIA_FORMAT_BMP,  ".bmp"),
        ]:
            try:
                wia_image = item.Transfer(fmt_guid)
                candidate = os.path.join(directorio, f"scan_{os.urandom(6).hex()}{suffix}")
                wia_image.SaveFile(candidate)
                tmp_path = candidate
                break
            except Exception:
                wia_image = None

        if tmp_path is None:
            raise RuntimeError("El escáner no pudo transferir la imagen en ningún formato soportado")

        # Normalize to PNG so the rest of the pipeline always receives a PNG
        if not tmp_path.endswith(".png"):
            png_path = tmp_path.rsplit(".", 1)[0] + ".png"
            with Image.open(tmp_path) as img:
                img.save(png_path, "PNG")
            os.remove(tmp_path)
            tmp_path = png_path

        return tmp_path
    finally:
        # Release every COM reference before CoUninitialize to avoid
        # "Win32 exception occurred releasing IUnknown" warnings
        wia_image = None
        item = None
        device = None
        dm = None
        gc.collect()
        pythoncom.CoUninitialize()
