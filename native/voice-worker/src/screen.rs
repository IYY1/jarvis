//! Screen capture for a display in HDR mode (`jarvis-voice --screen`).
//!
//! On such a display, Windows' 8-bit and 10-bit duplication formats clip everything
//! above 80 nits, so ordinary white is already gone. Only the 16-bit float format
//! keeps the picture, and FFmpeg has no light way to convert it: on the CPU that cost
//! 43% of a 16-thread machine at 60 fps. This reads the float frames and converts them
//! with a small shader on the GPU, then hands planar video to FFmpeg through a pipe.
//!
//! It says "ready" on stdout once it can capture, and starts when FFmpeg connects.
//! The mode ends when FFmpeg closes the pipe.
use crate::Result;

/// How many frames should exist `elapsed` seconds after the first one.
pub fn frames_due(elapsed: f64, fps: u32) -> u64 {
    (elapsed.max(0.0) * fps as f64) as u64 + 1
}

/// Send a new picture when one is due. With no new picture, wait a full frame for
/// one before repeating the last, so display and capture rates that match don't
/// beat against each other. FFmpeg counts frames as time, so the count must hold.
pub fn should_send(fresh: bool, due: u64, sent: u64) -> bool {
    if fresh {
        due > sent
    } else {
        due > sent + 1
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Plane {
    pub width: u32,
    pub height: u32,
    pub bytes: u32,
}

/// The luma and chroma planes of NV12 (8-bit) or P010 (10-bit).
pub fn planes(width: u32, height: u32, ten_bit: bool) -> [Plane; 2] {
    let (luma, chroma) = if ten_bit { (2, 4) } else { (1, 2) };
    [
        Plane {
            width,
            height,
            bytes: luma,
        },
        Plane {
            width: width / 2,
            height: height / 2,
            bytes: chroma,
        },
    ]
}

pub fn frame_size(width: u32, height: u32, ten_bit: bool) -> usize {
    planes(width, height, ten_bit)
        .iter()
        .map(|p| p.width as usize * p.height as usize * p.bytes as usize)
        .sum()
}

pub fn run(request: &serde_json::Value) -> Result<()> {
    let number = |key: &str| -> Result<u32> {
        request[key]
            .as_u64()
            .and_then(|v| u32::try_from(v).ok())
            .ok_or_else(|| format!("Screen capture needs a number for {key}").into())
    };
    let pipe = request["pipe"]
        .as_str()
        .filter(|p| p.starts_with(r"\\.\pipe\Jarvis-") && p.len() <= 240)
        .ok_or("Screen capture needs a private pipe")?;
    let (width, height, fps) = (number("width")?, number("height")?, number("fps")?);
    if width < 2 || height < 2 || width % 2 == 1 || height % 2 == 1 || !(1..=1000).contains(&fps) {
        return Err("Screen capture needs an even picture size and a sensible frame rate".into());
    }
    let (adapter, output) = (number("adapter")?, number("output")?);
    let hdr = request["hdr"].as_bool().unwrap_or(false);
    let ten_bit = hdr || request["ten_bit"].as_bool().unwrap_or(false);
    #[cfg(windows)]
    {
        windows_capture::capture(windows_capture::Job {
            pipe,
            adapter,
            output,
            width,
            height,
            fps,
            hdr,
            ten_bit,
        })
    }
    #[cfg(not(windows))]
    {
        let _ = (pipe, adapter, output, width, height, fps, hdr, ten_bit);
        Err("This screen capture is only implemented on Windows".into())
    }
}

#[cfg(windows)]
mod windows_capture {
    use super::{frame_size, frames_due, planes, should_send};
    use crate::Result;
    use std::collections::VecDeque;
    use std::time::Instant;
    use windows::core::{s, Interface, HSTRING, PCSTR};
    use windows::Win32::Devices::Display::{
        DisplayConfigGetDeviceInfo, GetDisplayConfigBufferSizes, QueryDisplayConfig,
        DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL, DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
        DISPLAYCONFIG_DEVICE_INFO_HEADER, DISPLAYCONFIG_MODE_INFO, DISPLAYCONFIG_PATH_INFO,
        DISPLAYCONFIG_SDR_WHITE_LEVEL, DISPLAYCONFIG_SOURCE_DEVICE_NAME, QDC_ONLY_ACTIVE_PATHS,
    };
    use windows::Win32::Foundation::{
        CloseHandle, LocalFree, ERROR_PIPE_CONNECTED, HANDLE, HLOCAL, HMODULE,
    };
    use windows::Win32::Graphics::Direct3D::Fxc::{D3DCompile, D3DCOMPILE_OPTIMIZATION_LEVEL3};
    use windows::Win32::Graphics::Direct3D::{
        ID3DBlob, D3D11_SRV_DIMENSION_TEXTURE2D, D3D_DRIVER_TYPE_UNKNOWN, D3D_FEATURE_LEVEL_11_0,
        D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
    };
    use windows::Win32::Graphics::Direct3D11::*;
    use windows::Win32::Graphics::Dxgi::Common::*;
    use windows::Win32::Graphics::Dxgi::*;
    use windows::Win32::Media::{timeBeginPeriod, timeEndPeriod};
    use windows::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
    use windows::Win32::Storage::FileSystem::{
        WriteFile, FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_OUTBOUND,
    };
    use windows::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
        PIPE_TYPE_BYTE, PIPE_WAIT,
    };
    use windows::Win32::System::Threading::{
        GetCurrentThread, SetThreadPriority, THREAD_PRIORITY_ABOVE_NORMAL,
    };
    use windows::Win32::UI::HiDpi::{
        SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    };

    pub struct Job<'a> {
        pub pipe: &'a str,
        pub adapter: u32,
        pub output: u32,
        pub width: u32,
        pub height: u32,
        pub fps: u32,
        pub hdr: bool,
        pub ten_bit: bool,
    }

    /// The picture Windows gives is linear BT.709 where 1.0 is 80 nits. "white" is
    /// where the user's "SDR content brightness" puts ordinary white.
    const SHADERS: &str = r#"
Texture2D scene : register(t0);
SamplerState smooth : register(s0);
cbuffer Settings : register(b0) { float white; float hdr; float ten_bit; float unused; };
struct V { float4 pos : SV_Position; float2 uv : TEXCOORD0; };

V vs(uint id : SV_VertexID) {
    V o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}

// Ordinary colors pass unchanged. Only what is brighter than nearly white bends,
// so a game's HDR highlights keep their shape instead of turning into flat patches.
float3 shoulder(float3 c) {
    const float knee = 0.85;
    float peak = max(c.r, max(c.g, c.b));
    if (peak <= knee) return c;
    float mapped = knee + (1 - knee) * (1 - exp(-(peak - knee) / (1 - knee)));
    float over = saturate((peak - 1) / 3);
    return lerp(c * mapped / peak, mapped.xxx, over * over);
}

float3 srgb(float3 c) {
    c = saturate(c);
    return (c <= 0.0031308) ? c * 12.92 : 1.055 * pow(c, 1 / 2.4) - 0.055;
}

float3 pq(float3 nits) {
    float3 y = pow(saturate(nits / 10000), 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * y) / (1 + 18.6875 * y), 78.84375);
}

float3 encode(float3 c) {
    if (hdr > 0.5) {
        const float3x3 to2020 = float3x3(0.6274, 0.3293, 0.0433, 0.0691, 0.9195, 0.0114, 0.0164, 0.0880, 0.8956);
        return pq(max(mul(to2020, c), 0) * 80);
    }
    return srgb(max(shoulder(c / white), 0));
}

float luma(float3 c) {
    return hdr > 0.5 ? dot(c, float3(0.2627, 0.678, 0.0593)) : dot(c, float3(0.2126, 0.7152, 0.0722));
}

// Limited range. P010 keeps its ten bits at the top of each 16-bit sample.
float y_code(float y) {
    return ten_bit > 0.5 ? (64 + 876 * y) * 64 / 65535 : (16 + 219 * y) / 255;
}
float c_code(float c) {
    return ten_bit > 0.5 ? (512 + 896 * c) * 64 / 65535 : (128 + 224 * c) / 255;
}

float ps_luma(V i) : SV_Target {
    return y_code(luma(encode(scene.Sample(smooth, i.uv).rgb)));
}

float2 ps_chroma(V i) : SV_Target {
    float3 c = encode(scene.Sample(smooth, i.uv).rgb);
    float y = luma(c);
    float2 d = hdr > 0.5 ? float2((c.b - y) / 1.8814, (c.r - y) / 1.4746) : float2((c.b - y) / 1.8556, (c.r - y) / 1.5748);
    return float2(c_code(d.x), c_code(d.y));
}

// The pointer is an ordinary sRGB image, as bright as ordinary white.
float4 ps_pointer(V i) : SV_Target {
    float4 p = scene.Sample(smooth, i.uv);
    float3 linear_ = (p.rgb <= 0.04045) ? p.rgb / 12.92 : pow((p.rgb + 0.055) / 1.055, 2.4);
    return float4(linear_ * white, p.a);
}
"#;

    fn compile(entry: PCSTR, target: PCSTR) -> Result<ID3DBlob> {
        let mut code = None;
        let mut errors = None;
        let done = unsafe {
            D3DCompile(
                SHADERS.as_ptr() as *const _,
                SHADERS.len(),
                PCSTR::null(),
                None,
                None,
                entry,
                target,
                D3DCOMPILE_OPTIMIZATION_LEVEL3,
                0,
                &mut code,
                Some(&mut errors),
            )
        };
        if let Err(error) = done {
            let detail = errors
                .map(|blob: ID3DBlob| unsafe {
                    String::from_utf8_lossy(std::slice::from_raw_parts(
                        blob.GetBufferPointer() as *const u8,
                        blob.GetBufferSize(),
                    ))
                    .into_owned()
                })
                .unwrap_or_default();
            return Err(format!("Screen shader failed: {error} {detail}").into());
        }
        Ok(code.ok_or("Screen shader produced nothing")?)
    }

    fn bytes(blob: &ID3DBlob) -> &[u8] {
        unsafe {
            std::slice::from_raw_parts(blob.GetBufferPointer() as *const u8, blob.GetBufferSize())
        }
    }

    /// Where the user's "SDR content brightness" puts ordinary white, 1.0 = 80 nits.
    fn sdr_white(device_name: &[u16]) -> f32 {
        unsafe {
            let (mut paths, mut modes) = (0, 0);
            if GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &mut paths, &mut modes).is_err() {
                return 3.0;
            }
            let mut path = vec![DISPLAYCONFIG_PATH_INFO::default(); paths as usize];
            let mut mode = vec![DISPLAYCONFIG_MODE_INFO::default(); modes as usize];
            if QueryDisplayConfig(
                QDC_ONLY_ACTIVE_PATHS,
                &mut paths,
                path.as_mut_ptr(),
                &mut modes,
                mode.as_mut_ptr(),
                None,
            )
            .is_err()
            {
                return 3.0;
            }
            for item in &path[..paths as usize] {
                let mut source = DISPLAYCONFIG_SOURCE_DEVICE_NAME::default();
                source.header = DISPLAYCONFIG_DEVICE_INFO_HEADER {
                    r#type: DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
                    size: std::mem::size_of::<DISPLAYCONFIG_SOURCE_DEVICE_NAME>() as u32,
                    adapterId: item.sourceInfo.adapterId,
                    id: item.sourceInfo.id,
                };
                if DisplayConfigGetDeviceInfo(&mut source.header) != 0 {
                    continue;
                }
                let wanted = device_name.iter().take_while(|c| **c != 0);
                let found = source.viewGdiDeviceName.iter().take_while(|c| **c != 0);
                if !wanted.eq(found) {
                    continue;
                }
                let mut level = DISPLAYCONFIG_SDR_WHITE_LEVEL::default();
                level.header = DISPLAYCONFIG_DEVICE_INFO_HEADER {
                    r#type: DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                    size: std::mem::size_of::<DISPLAYCONFIG_SDR_WHITE_LEVEL>() as u32,
                    adapterId: item.targetInfo.adapterId,
                    id: item.targetInfo.id,
                };
                if DisplayConfigGetDeviceInfo(&mut level.header) == 0 && level.SDRWhiteLevel > 0 {
                    return level.SDRWhiteLevel as f32 / 1000.0;
                }
            }
        }
        3.0
    }

    fn private_pipe(name: &str, frame_bytes: usize) -> Result<HANDLE> {
        unsafe {
            // Only the owner: this pipe carries the user's screen.
            let mut descriptor = PSECURITY_DESCRIPTOR::default();
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                &HSTRING::from("D:P(A;;GA;;;OW)"),
                SDDL_REVISION_1,
                &mut descriptor,
                None,
            )?;
            let attributes = SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor.0,
                bInheritHandle: false.into(),
            };
            let handle = CreateNamedPipeW(
                &HSTRING::from(name),
                PIPE_ACCESS_OUTBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                (frame_bytes as u32).min(16 * 1024 * 1024),
                0,
                0,
                Some(&attributes),
            );
            let _ = LocalFree(Some(HLOCAL(descriptor.0)));
            if handle.is_invalid() {
                return Err(windows::core::Error::from_thread().into());
            }
            Ok(handle)
        }
    }

    struct Gpu {
        device: ID3D11Device,
        context: ID3D11DeviceContext,
        output: IDXGIOutput5,
        vertex: ID3D11VertexShader,
        luma: ID3D11PixelShader,
        chroma: ID3D11PixelShader,
        pointer: ID3D11PixelShader,
        sampler: ID3D11SamplerState,
        settings: ID3D11Buffer,
        blend: ID3D11BlendState,
    }

    fn open_gpu(job: &Job) -> Result<(Gpu, f32)> {
        unsafe {
            let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
            let adapter = factory.EnumAdapters1(job.adapter)?;
            let output = adapter.EnumOutputs(job.output)?;
            let description = output.GetDesc()?;
            if description.Rotation != DXGI_MODE_ROTATION_IDENTITY
                && description.Rotation != DXGI_MODE_ROTATION_UNSPECIFIED
            {
                return Err("Capture of a rotated display in HDR mode isn't supported. Turn off HDR on it, or use it unrotated.".into());
            }
            let white = sdr_white(&description.DeviceName);
            let output: IDXGIOutput5 = output.cast()?;
            let mut device = None;
            let mut context = None;
            D3D11CreateDevice(
                &adapter,
                D3D_DRIVER_TYPE_UNKNOWN,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_FLAG(0),
                Some(&[D3D_FEATURE_LEVEL_11_0]),
                D3D11_SDK_VERSION,
                Some(&mut device),
                None,
                Some(&mut context),
            )?;
            let device: ID3D11Device = device.ok_or("No graphics device")?;
            let context = context.ok_or("No graphics context")?;
            // A game can fill the GPU's queue; this work is tiny and must not wait behind it.
            if let Ok(dxgi) = device.cast::<IDXGIDevice>() {
                let _ = dxgi.SetGPUThreadPriority(7);
            }
            let vs = compile(s!("vs"), s!("vs_5_0"))?;
            let (luma, chroma, pointer) = (
                compile(s!("ps_luma"), s!("ps_5_0"))?,
                compile(s!("ps_chroma"), s!("ps_5_0"))?,
                compile(s!("ps_pointer"), s!("ps_5_0"))?,
            );
            let mut vertex = None;
            device.CreateVertexShader(bytes(&vs), None, Some(&mut vertex))?;
            let pixel = |blob: &ID3DBlob| -> Result<ID3D11PixelShader> {
                let mut shader = None;
                device.CreatePixelShader(bytes(blob), None, Some(&mut shader))?;
                Ok(shader.ok_or("No pixel shader")?)
            };
            let (luma, chroma, pointer) = (pixel(&luma)?, pixel(&chroma)?, pixel(&pointer)?);
            let mut sampler = None;
            device.CreateSamplerState(
                &D3D11_SAMPLER_DESC {
                    Filter: D3D11_FILTER_MIN_MAG_MIP_LINEAR,
                    AddressU: D3D11_TEXTURE_ADDRESS_CLAMP,
                    AddressV: D3D11_TEXTURE_ADDRESS_CLAMP,
                    AddressW: D3D11_TEXTURE_ADDRESS_CLAMP,
                    MaxLOD: f32::MAX,
                    ..Default::default()
                },
                Some(&mut sampler),
            )?;
            let values: [f32; 4] = [
                white,
                job.hdr as u32 as f32,
                job.ten_bit as u32 as f32,
                0.0,
            ];
            let mut settings = None;
            device.CreateBuffer(
                &D3D11_BUFFER_DESC {
                    ByteWidth: 16,
                    Usage: D3D11_USAGE_IMMUTABLE,
                    BindFlags: D3D11_BIND_CONSTANT_BUFFER.0 as u32,
                    ..Default::default()
                },
                Some(&D3D11_SUBRESOURCE_DATA {
                    pSysMem: values.as_ptr() as *const _,
                    ..Default::default()
                }),
                Some(&mut settings),
            )?;
            let mut blend_desc = D3D11_BLEND_DESC::default();
            blend_desc.RenderTarget[0] = D3D11_RENDER_TARGET_BLEND_DESC {
                BlendEnable: true.into(),
                SrcBlend: D3D11_BLEND_SRC_ALPHA,
                DestBlend: D3D11_BLEND_INV_SRC_ALPHA,
                BlendOp: D3D11_BLEND_OP_ADD,
                SrcBlendAlpha: D3D11_BLEND_ONE,
                DestBlendAlpha: D3D11_BLEND_ZERO,
                BlendOpAlpha: D3D11_BLEND_OP_ADD,
                RenderTargetWriteMask: D3D11_COLOR_WRITE_ENABLE_ALL.0 as u8,
            };
            let mut blend = None;
            device.CreateBlendState(&blend_desc, Some(&mut blend))?;
            Ok((
                Gpu {
                    context,
                    output,
                    vertex: vertex.ok_or("No vertex shader")?,
                    luma,
                    chroma,
                    pointer,
                    sampler: sampler.ok_or("No sampler")?,
                    settings: settings.ok_or("No settings buffer")?,
                    blend: blend.ok_or("No blend state")?,
                    device,
                },
                white,
            ))
        }
    }

    fn texture(
        gpu: &Gpu,
        width: u32,
        height: u32,
        format: DXGI_FORMAT,
        bind: D3D11_BIND_FLAG,
    ) -> Result<ID3D11Texture2D> {
        let mut made = None;
        unsafe {
            gpu.device.CreateTexture2D(
                &D3D11_TEXTURE2D_DESC {
                    Width: width,
                    Height: height,
                    MipLevels: 1,
                    ArraySize: 1,
                    Format: format,
                    SampleDesc: DXGI_SAMPLE_DESC {
                        Count: 1,
                        Quality: 0,
                    },
                    Usage: D3D11_USAGE_DEFAULT,
                    BindFlags: bind.0 as u32,
                    ..Default::default()
                },
                None,
                Some(&mut made),
            )?;
        }
        Ok(made.ok_or("No texture")?)
    }

    fn staging(gpu: &Gpu, width: u32, height: u32, format: DXGI_FORMAT) -> Result<ID3D11Texture2D> {
        let mut made = None;
        unsafe {
            gpu.device.CreateTexture2D(
                &D3D11_TEXTURE2D_DESC {
                    Width: width,
                    Height: height,
                    MipLevels: 1,
                    ArraySize: 1,
                    Format: format,
                    SampleDesc: DXGI_SAMPLE_DESC {
                        Count: 1,
                        Quality: 0,
                    },
                    Usage: D3D11_USAGE_STAGING,
                    CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
                    ..Default::default()
                },
                None,
                Some(&mut made),
            )?;
        }
        Ok(made.ok_or("No staging texture")?)
    }

    fn view(gpu: &Gpu, made: &ID3D11Texture2D, format: DXGI_FORMAT) -> Result<ID3D11ShaderResourceView> {
        let mut result = None;
        unsafe {
            gpu.device.CreateShaderResourceView(
                made,
                Some(&D3D11_SHADER_RESOURCE_VIEW_DESC {
                    Format: format,
                    ViewDimension: D3D11_SRV_DIMENSION_TEXTURE2D,
                    Anonymous: D3D11_SHADER_RESOURCE_VIEW_DESC_0 {
                        Texture2D: D3D11_TEX2D_SRV {
                            MostDetailedMip: 0,
                            MipLevels: 1,
                        },
                    },
                }),
                Some(&mut result),
            )?;
        }
        Ok(result.ok_or("No texture view")?)
    }

    fn target(gpu: &Gpu, made: &ID3D11Texture2D) -> Result<ID3D11RenderTargetView> {
        let mut result = None;
        unsafe {
            gpu.device
                .CreateRenderTargetView(made, None, Some(&mut result))?;
        }
        Ok(result.ok_or("No render target")?)
    }

    /// Everything sized to the desktop, remade when the display mode changes.
    struct Desk {
        /// None only while waiting to get the desktop back: Windows wants the lost
        /// duplication released before a new one is made.
        duplication: Option<IDXGIOutputDuplication>,
        width: u32,
        height: u32,
        scene: ID3D11Texture2D,
        scene_view: ID3D11ShaderResourceView,
        /// The scene with the pointer on it. Games usually hide the pointer, so this
        /// full-size picture is only made once one has to be drawn.
        composed: Option<Composed>,
    }

    struct Composed {
        image: ID3D11Texture2D,
        view: ID3D11ShaderResourceView,
        target: ID3D11RenderTargetView,
    }

    const FLOAT: DXGI_FORMAT = DXGI_FORMAT_R16G16B16A16_FLOAT;

    /// Duplication works only from the desktop the user sees. After the secure desktop
    /// (a permission prompt, Ctrl+Alt+Del) this thread must be moved back to it, or
    /// every new duplication is lost at once.
    fn attach_input_desktop() {
        use windows::Win32::System::StationsAndDesktops::{
            CloseDesktop, OpenInputDesktop, SetThreadDesktop, DESKTOP_ACCESS_FLAGS,
            DESKTOP_CONTROL_FLAGS,
        };
        const GENERIC_ALL: u32 = 0x1000_0000;
        unsafe {
            if let Ok(desktop) =
                OpenInputDesktop(DESKTOP_CONTROL_FLAGS(0), false, DESKTOP_ACCESS_FLAGS(GENERIC_ALL))
            {
                let _ = SetThreadDesktop(desktop);
                let _ = CloseDesktop(desktop);
            }
        }
    }

    fn open_desk(gpu: &Gpu) -> Result<Desk> {
        unsafe {
            let duplication = gpu.output.DuplicateOutput1(&gpu.device, 0, &[FLOAT])?;
            let mode = duplication.GetDesc().ModeDesc;
            let (width, height) = (mode.Width, mode.Height);
            let scene = texture(gpu, width, height, FLOAT, D3D11_BIND_SHADER_RESOURCE)?;
            Ok(Desk {
                scene_view: view(gpu, &scene, FLOAT)?,
                duplication: Some(duplication),
                width,
                height,
                scene,
                composed: None,
            })
        }
    }

    fn open_composed(gpu: &Gpu, width: u32, height: u32) -> Result<Composed> {
        let both = D3D11_BIND_FLAG(D3D11_BIND_SHADER_RESOURCE.0 | D3D11_BIND_RENDER_TARGET.0);
        let image = texture(gpu, width, height, FLOAT, both)?;
        Ok(Composed {
            view: view(gpu, &image, FLOAT)?,
            target: target(gpu, &image)?,
            image,
        })
    }

    struct Pointer {
        visible: bool,
        x: i32,
        y: i32,
        width: u32,
        height: u32,
        view: Option<ID3D11ShaderResourceView>,
    }

    /// DXGI's three pointer shapes as one straight-alpha BGRA image. A real "invert"
    /// pixel would need the picture under it, so those become solid black.
    fn pointer_pixels(
        kind: u32,
        width: u32,
        height: u32,
        pitch: u32,
        data: &[u8],
    ) -> (u32, Vec<u8>) {
        let mut out = Vec::new();
        if kind == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME.0 as u32 {
            let rows = height / 2;
            for y in 0..rows as usize {
                for x in 0..width as usize {
                    let bit = 0x80u8 >> (x % 8);
                    let and = data[y * pitch as usize + x / 8] & bit != 0;
                    let xor = data[(y + rows as usize) * pitch as usize + x / 8] & bit != 0;
                    out.extend_from_slice(match (and, xor) {
                        (false, false) => &[0, 0, 0, 255],
                        (false, true) => &[255, 255, 255, 255],
                        (true, false) => &[0, 0, 0, 0],
                        (true, true) => &[0, 0, 0, 255],
                    });
                }
            }
            return (rows, out);
        }
        for y in 0..height as usize {
            for x in 0..width as usize {
                let at = y * pitch as usize + x * 4;
                let (b, g, r, a) = (data[at], data[at + 1], data[at + 2], data[at + 3]);
                if kind == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR.0 as u32 {
                    // Alpha is the mask here: 0 replaces the screen, 255 combines with it.
                    let hidden = a != 0 && b == 0 && g == 0 && r == 0;
                    let shown = if a != 0 && !hidden {
                        [0, 0, 0, 255]
                    } else if hidden {
                        [0, 0, 0, 0]
                    } else {
                        [b, g, r, 255]
                    };
                    out.extend_from_slice(&shown);
                } else {
                    out.extend_from_slice(&[b, g, r, a]);
                }
            }
        }
        (height, out)
    }

    fn update_pointer(gpu: &Gpu, desk: &Desk, info: &DXGI_OUTDUPL_FRAME_INFO, pointer: &mut Pointer) {
        if info.LastMouseUpdateTime == 0 {
            return;
        }
        pointer.visible = info.PointerPosition.Visible.as_bool();
        pointer.x = info.PointerPosition.Position.x;
        pointer.y = info.PointerPosition.Position.y;
        if info.PointerShapeBufferSize == 0 {
            return;
        }
        let mut data = vec![0u8; info.PointerShapeBufferSize as usize];
        let mut needed = 0;
        let mut shape = DXGI_OUTDUPL_POINTER_SHAPE_INFO::default();
        let Some(duplication) = &desk.duplication else {
            return;
        };
        if unsafe {
            duplication.GetFramePointerShape(
                data.len() as u32,
                data.as_mut_ptr() as *mut _,
                &mut needed,
                &mut shape,
            )
        }
        .is_err()
        {
            return;
        }
        let (rows, pixels) =
            pointer_pixels(shape.Type, shape.Width, shape.Height, shape.Pitch, &data);
        let mut made = None;
        let created = unsafe {
            gpu.device.CreateTexture2D(
                &D3D11_TEXTURE2D_DESC {
                    Width: shape.Width,
                    Height: rows,
                    MipLevels: 1,
                    ArraySize: 1,
                    Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                    SampleDesc: DXGI_SAMPLE_DESC {
                        Count: 1,
                        Quality: 0,
                    },
                    Usage: D3D11_USAGE_IMMUTABLE,
                    BindFlags: D3D11_BIND_SHADER_RESOURCE.0 as u32,
                    ..Default::default()
                },
                Some(&D3D11_SUBRESOURCE_DATA {
                    pSysMem: pixels.as_ptr() as *const _,
                    SysMemPitch: shape.Width * 4,
                    SysMemSlicePitch: 0,
                }),
                Some(&mut made),
            )
        };
        if created.is_ok() {
            if let Some(made) = made {
                pointer.view = view(gpu, &made, DXGI_FORMAT_B8G8R8A8_UNORM).ok();
                pointer.width = shape.Width;
                pointer.height = rows;
            }
        }
    }

    /// The output picture's two planes, and three frames of read-back so the GPU is
    /// never waited for: reading always takes the oldest, finished one.
    struct Output {
        targets: [ID3D11RenderTargetView; 2],
        textures: [ID3D11Texture2D; 2],
        rings: Vec<[ID3D11Texture2D; 2]>,
    }

    const RING: usize = 3;

    fn open_output(gpu: &Gpu, job: &Job) -> Result<Output> {
        let formats = if job.ten_bit {
            [DXGI_FORMAT_R16_UNORM, DXGI_FORMAT_R16G16_UNORM]
        } else {
            [DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_R8G8_UNORM]
        };
        let plan = planes(job.width, job.height, job.ten_bit);
        let make = |i: usize| texture(gpu, plan[i].width, plan[i].height, formats[i], D3D11_BIND_RENDER_TARGET);
        let textures = [make(0)?, make(1)?];
        let targets = [target(gpu, &textures[0])?, target(gpu, &textures[1])?];
        let mut rings = Vec::new();
        for _ in 0..RING {
            rings.push([
                staging(gpu, plan[0].width, plan[0].height, formats[0])?,
                staging(gpu, plan[1].width, plan[1].height, formats[1])?,
            ]);
        }
        Ok(Output {
            targets,
            textures,
            rings,
        })
    }

    fn viewport(x: f32, y: f32, width: f32, height: f32) -> D3D11_VIEWPORT {
        D3D11_VIEWPORT {
            TopLeftX: x,
            TopLeftY: y,
            Width: width,
            Height: height,
            MinDepth: 0.0,
            MaxDepth: 1.0,
        }
    }

    fn prepare(gpu: &Gpu) {
        unsafe {
            let c = &gpu.context;
            c.IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            c.VSSetShader(&gpu.vertex, None);
            c.PSSetSamplers(0, Some(&[Some(gpu.sampler.clone())]));
            c.PSSetConstantBuffers(0, Some(&[Some(gpu.settings.clone())]));
        }
    }

    fn draw_pointer(gpu: &Gpu, desk: &mut Desk, pointer: &Pointer) -> Result<()> {
        let Some(image) = &pointer.view else {
            return Ok(());
        };
        if desk.composed.is_none() {
            desk.composed = Some(open_composed(gpu, desk.width, desk.height)?);
        }
        let composed = desk.composed.as_ref().ok_or("No composed picture")?;
        unsafe {
            let c = &gpu.context;
            c.CopyResource(&composed.image, &desk.scene);
            c.OMSetRenderTargets(Some(&[Some(composed.target.clone())]), None);
            c.OMSetBlendState(&gpu.blend, None, 0xffffffff);
            c.RSSetViewports(Some(&[viewport(
                pointer.x as f32,
                pointer.y as f32,
                pointer.width as f32,
                pointer.height as f32,
            )]));
            c.PSSetShader(&gpu.pointer, None);
            c.PSSetShaderResources(0, Some(&[Some(image.clone())]));
            c.Draw(3, 0);
            c.PSSetShaderResources(0, Some(&[None]));
            c.OMSetBlendState(None, None, 0xffffffff);
            c.OMSetRenderTargets(Some(&[None]), None);
        }
        Ok(())
    }

    fn draw_planes(gpu: &Gpu, source: &ID3D11ShaderResourceView, output: &Output, job: &Job, slot: usize) {
        let plan = planes(job.width, job.height, job.ten_bit);
        unsafe {
            let c = &gpu.context;
            c.PSSetShaderResources(0, Some(&[Some(source.clone())]));
            for (index, shader) in [&gpu.luma, &gpu.chroma].into_iter().enumerate() {
                c.OMSetRenderTargets(Some(&[Some(output.targets[index].clone())]), None);
                c.RSSetViewports(Some(&[viewport(
                    0.0,
                    0.0,
                    plan[index].width as f32,
                    plan[index].height as f32,
                )]));
                c.PSSetShader(shader, None);
                c.Draw(3, 0);
            }
            c.OMSetRenderTargets(Some(&[None]), None);
            c.PSSetShaderResources(0, Some(&[None]));
            for index in 0..2 {
                c.CopyResource(&output.rings[slot][index], &output.textures[index]);
            }
        }
    }

    fn read_back(gpu: &Gpu, output: &Output, job: &Job, slot: usize, frame: &mut [u8]) -> Result<()> {
        let plan = planes(job.width, job.height, job.ten_bit);
        let mut at = 0;
        for (index, plane) in plan.iter().enumerate() {
            let row = (plane.width * plane.bytes) as usize;
            unsafe {
                let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
                gpu.context.Map(
                    &output.rings[slot][index],
                    0,
                    D3D11_MAP_READ,
                    0,
                    Some(&mut mapped),
                )?;
                for y in 0..plane.height as usize {
                    std::ptr::copy_nonoverlapping(
                        (mapped.pData as *const u8).add(y * mapped.RowPitch as usize),
                        frame[at..].as_mut_ptr(),
                        row,
                    );
                    at += row;
                }
                gpu.context.Unmap(&output.rings[slot][index], 0);
            }
        }
        Ok(())
    }

    /// False once FFmpeg has closed the pipe, which is how capture ends.
    fn send(pipe: HANDLE, frame: &[u8]) -> bool {
        let mut left = frame;
        while !left.is_empty() {
            let mut written = 0;
            if unsafe { WriteFile(pipe, Some(left), Some(&mut written), None) }.is_err() {
                return false;
            }
            left = &left[written as usize..];
        }
        true
    }

    pub fn capture(job: Job) -> Result<()> {
        unsafe {
            let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            let _ = SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
            timeBeginPeriod(1);
        }
        let result = capture_loop(&job);
        unsafe {
            timeEndPeriod(1);
        }
        result
    }

    fn capture_loop(job: &Job) -> Result<()> {
        let size = frame_size(job.width, job.height, job.ten_bit);
        let pipe = private_pipe(job.pipe, size)?;
        let result = run_on_pipe(job, pipe, size);
        unsafe {
            let _ = CloseHandle(pipe);
        }
        result
    }

    fn run_on_pipe(job: &Job, pipe: HANDLE, size: usize) -> Result<()> {
        let (mut gpu, _white) = open_gpu(job)?;
        let mut desk = open_desk(&gpu)?;
        let mut output = open_output(&gpu, job)?;
        prepare(&gpu);
        let mut frame = vec![0u8; size];
        let mut pointer = Pointer {
            visible: false,
            x: 0,
            y: 0,
            width: 0,
            height: 0,
            view: None,
        };
        println!("ready");
        use std::io::Write;
        std::io::stdout().flush()?;
        if let Err(error) = unsafe { ConnectNamedPipe(pipe, None) } {
            if error.code() != ERROR_PIPE_CONNECTED.to_hresult() {
                return Err(error.into());
            }
        }

        let start = Instant::now();
        let period = 1.0 / job.fps as f64;
        let mut sent: u64 = 0;
        let mut next_slot = 0;
        let mut waiting: VecDeque<usize> = VecDeque::new();
        let mut fresh = true; // the first frame is sent however the desktop looks
        // Reopened, then lost again before a single frame: back off, and after a few
        // in a row start over with a new device, as Microsoft's duplication sample does.
        let mut quick_losses: u32 = 0;
        let mut since_reopen: u32 = 1;
        // JARVIS_SCREEN_TRACE=1: every two seconds, what Desktop Duplication did.
        let trace = std::env::var_os("JARVIS_SCREEN_TRACE").is_some();
        let (mut traced, mut lost, mut reopened_count, mut failed, mut acquired) =
            (Instant::now(), 0u32, 0u32, 0u32, 0u32);
        let mut last_error = String::new();
        loop {
            if trace && traced.elapsed().as_secs() >= 2 {
                eprintln!(
                    "{:.0}s acquired {acquired} lost {lost} reopened {reopened_count} reopen-failed {failed} last {last_error}",
                    start.elapsed().as_secs_f64()
                );
                traced = Instant::now();
                (acquired, lost, reopened_count, failed) = (0, 0, 0, 0);
            }
            let duplication = desk.duplication.clone().ok_or("Screen capture lost its desktop")?;
            let until_repeat = (sent + 1) as f64 * period - start.elapsed().as_secs_f64();
            let wait = (until_repeat * 1000.0).ceil().clamp(1.0, 50.0) as u32;
            let mut info = DXGI_OUTDUPL_FRAME_INFO::default();
            let mut resource = None;
            match unsafe { duplication.AcquireNextFrame(wait, &mut info, &mut resource) } {
                Ok(()) => {
                    acquired += 1;
                    since_reopen += 1;
                    update_pointer(&gpu, &desk, &info, &mut pointer);
                    if info.LastPresentTime != 0 {
                        if let Some(image) = &resource {
                            if let Ok(image) = image.cast::<ID3D11Texture2D>() {
                                unsafe { gpu.context.CopyResource(&desk.scene, &image) };
                            }
                        }
                    }
                    if info.LastPresentTime != 0 || info.LastMouseUpdateTime != 0 {
                        fresh = true;
                    }
                    drop(resource);
                    unsafe {
                        let _ = duplication.ReleaseFrame();
                    }
                }
                Err(error) if error.code() == DXGI_ERROR_WAIT_TIMEOUT => {}
                Err(error)
                    if error.code() == DXGI_ERROR_ACCESS_LOST
                        || error.code() == DXGI_ERROR_INVALID_CALL =>
                {
                    lost += 1;
                    last_error = format!("{:?}", error.code());
                    drop(duplication);
                    desk.duplication = None;
                    quick_losses = if since_reopen == 0 { quick_losses + 1 } else { 0 };
                    if quick_losses > 0 {
                        // Paced below by holding frames; at most a quarter second.
                        let pause = (10u64 << quick_losses.min(5)).min(250);
                        std::thread::sleep(std::time::Duration::from_millis(pause));
                    }
                    // A mode change, or the secure desktop. Hold the last picture
                    // until Windows lets us back, keeping the frame count honest.
                    // Pictures already counted go first, in order.
                    while let Some(slot) = waiting.pop_front() {
                        read_back(&gpu, &output, job, slot, &mut frame)?;
                        if !send(pipe, &frame) {
                            return Ok(());
                        }
                    }
                    loop {
                        let due = frames_due(start.elapsed().as_secs_f64(), job.fps);
                        while due > sent + 1 {
                            if !send(pipe, &frame) {
                                return Ok(());
                            }
                            sent += 1;
                        }
                        attach_input_desktop();
                        if quick_losses >= 4 {
                            // Start over on a new device. Its staging ring is new too.
                            waiting.clear();
                            let (renewed, _) = open_gpu(job)?;
                            gpu = renewed;
                            output = open_output(&gpu, job)?;
                            prepare(&gpu);
                            quick_losses = 0;
                        }
                        match open_desk(&gpu) {
                            Ok(reopened) => {
                                reopened_count += 1;
                                since_reopen = 0;
                                desk = reopened;
                                pointer.view = None;
                                pointer.visible = false;
                                break;
                            }
                            Err(error) => {
                                failed += 1;
                                last_error = error.to_string();
                                std::thread::sleep(std::time::Duration::from_millis(100))
                            }
                        }
                    }
                    continue;
                }
                Err(error) => return Err(format!("Screen capture stopped: {error}").into()),
            }

            let due = frames_due(start.elapsed().as_secs_f64(), job.fps);
            if fresh && should_send(true, due, sent) {
                if pointer.visible && pointer.view.is_some() {
                    draw_pointer(&gpu, &mut desk, &pointer)?;
                }
                let source = match &desk.composed {
                    Some(composed) if pointer.visible && pointer.view.is_some() => &composed.view,
                    _ => &desk.scene_view,
                };
                draw_planes(&gpu, source, &output, job, next_slot);
                waiting.push_back(next_slot);
                next_slot = (next_slot + 1) % RING;
                sent += 1;
                fresh = false;
                if waiting.len() >= RING - 1 {
                    let slot = waiting.pop_front().ok_or("Lost track of a frame")?;
                    read_back(&gpu, &output, job, slot, &mut frame)?;
                    if !send(pipe, &frame) {
                        return Ok(());
                    }
                }
            }
            // Nothing new on screen, or we fell behind: keep the count with what we have.
            // Pictures still on the GPU were counted when drawn, so they go out first
            // without counting again; only the repeat of the newest one counts. (Counting
            // them twice left a still screen at 37 frames a second of 60, and FFmpeg
            // then stopped taking sound until the audio worker gave up.)
            let due = frames_due(start.elapsed().as_secs_f64(), job.fps);
            if should_send(false, due, sent) {
                while let Some(slot) = waiting.pop_front() {
                    read_back(&gpu, &output, job, slot, &mut frame)?;
                    if !send(pipe, &frame) {
                        return Ok(());
                    }
                }
            }
            while should_send(false, due, sent) {
                if !send(pipe, &frame) {
                    return Ok(());
                }
                sent += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{frame_size, frames_due, planes, should_send};

    #[test]
    fn the_frame_count_follows_time() {
        assert_eq!(frames_due(0.0, 60), 1);
        assert_eq!(frames_due(0.999, 60), 60);
        assert_eq!(frames_due(1.0, 60), 61);
        assert_eq!(frames_due(-1.0, 60), 1);
    }

    #[test]
    fn new_pictures_go_when_due_and_repeats_only_when_late() {
        assert!(should_send(true, 5, 4));
        assert!(!should_send(true, 4, 4)); // ahead of time: keep it for the next frame
        assert!(!should_send(false, 5, 4)); // a new picture may still come this frame
        assert!(should_send(false, 6, 4));
    }

    #[test]
    fn plane_sizes_match_what_ffmpeg_reads() {
        assert_eq!(frame_size(2560, 1440, false), 2560 * 1440 * 3 / 2);
        assert_eq!(frame_size(2560, 1440, true), 2560 * 1440 * 3);
        let [luma, chroma] = planes(1920, 1080, true);
        assert_eq!((luma.width, luma.bytes), (1920, 2));
        assert_eq!((chroma.width, chroma.height, chroma.bytes), (960, 540, 4));
    }
}
