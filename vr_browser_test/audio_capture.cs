// audio_capture.exe <pid> [--mute <flagfile>]   |   audio_capture.exe --unmute
// Captures the sound of ONE process tree (Brave) with the Windows 10 2004+ "process loopback" API and writes
// raw PCM (48 kHz, stereo, signed 16-bit little endian) to stdout until stdin closes.
// --mute: mutes the laptop's default output while capturing (the capture is taken before the speakers, so the
// phone still gets the sound) and unmutes it again on exit. Only if it was not muted already; <flagfile> exists
// while it is muted by us, so a crash can be repaired with --unmute (server.py does that at start).
// Built by server.py with the csc.exe that ships with Windows (.NET Framework 4):
//   csc /nologo /optimize /out:audio_capture.exe audio_capture.cs
// Only sound made by that process tree is captured (not other apps, not the microphone).
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

static class Program
{
    const int SAMPLE_RATE = 48000, CHANNELS = 2, BITS = 16;

    [StructLayout(LayoutKind.Sequential)]
    struct WAVEFORMATEX
    {
        public ushort wFormatTag, nChannels; public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct AUDIOCLIENT_ACTIVATION_PARAMS
    {
        public int ActivationType;          // 1 = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        public uint TargetProcessId;
        public int ProcessLoopbackMode;     // 0 = include target process tree
    }

    [StructLayout(LayoutKind.Sequential)]
    struct BLOB_PROPVARIANT
    {
        public ushort vt, r1, r2, r3;       // vt = VT_BLOB (65)
        public uint cbSize; public IntPtr pBlobData;
        public IntPtr pad;
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity,
                                     ref WAVEFORMATEX format, IntPtr audioSessionGuid);
        [PreserveSig] int GetBufferSize(out uint frames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long def, out long min);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr h);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devPos, out ulong qpcPos);
        [PreserveSig] int ReleaseBuffer(uint frames);
        [PreserveSig] int GetNextPacketSize(out uint frames);
    }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        [PreserveSig] int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    [ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }

    [ClassInterface(ClassInterfaceType.None)]
    class Handler : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public int Hr = -1; public object Iface;
        public void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op)
        {
            int hr; object o;
            int r = op.GetActivateResult(out hr, out o);
            Hr = r != 0 ? r : hr; Iface = o;
            Done.Set();
        }
    }

    [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = true)]
    static extern int ActivateAudioInterfaceAsync([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid iid,
        IntPtr activationParams, IActivateAudioInterfaceCompletionHandler handler,
        out IActivateAudioInterfaceAsyncOperation op);

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCo { }
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(int flow, int mask, out IntPtr devs);
        [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice dev);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice
    {
        [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o);
    }
    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume
    {
        [PreserveSig] int RegisterControlChangeNotify(IntPtr n); [PreserveSig] int UnregisterControlChangeNotify(IntPtr n);
        [PreserveSig] int GetChannelCount(out uint c); [PreserveSig] int SetMasterVolumeLevel(float l, IntPtr g);
        [PreserveSig] int SetMasterVolumeLevelScalar(float l, IntPtr g); [PreserveSig] int GetMasterVolumeLevel(out float l);
        [PreserveSig] int GetMasterVolumeLevelScalar(out float l); [PreserveSig] int SetChannelVolumeLevel(uint c, float l, IntPtr g);
        [PreserveSig] int SetChannelVolumeLevelScalar(uint c, float l, IntPtr g); [PreserveSig] int GetChannelVolumeLevel(uint c, out float l);
        [PreserveSig] int GetChannelVolumeLevelScalar(uint c, out float l);
        [PreserveSig] int SetMute(bool m, IntPtr g); [PreserveSig] int GetMute(out bool m);
    }

    static IAudioEndpointVolume Speakers()
    {
        var en = (IMMDeviceEnumerator)new MMDeviceEnumeratorCo();
        IMMDevice dev;
        if (en.GetDefaultAudioEndpoint(0, 1, out dev) != 0 || dev == null) return null;   // eRender, eMultimedia
        var iid = typeof(IAudioEndpointVolume).GUID; object o;
        if (dev.Activate(ref iid, 23, IntPtr.Zero, out o) != 0) return null;
        return (IAudioEndpointVolume)o;
    }

    [DllImport("kernel32.dll")] static extern IntPtr CreateEventW(IntPtr a, bool manual, bool init, IntPtr name);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h, uint ms);

    [MTAThread]
    static int Main(string[] args)
    {
        if (args.Length >= 1 && args[0] == "--unmute")
        {
            try { var v = Speakers(); if (v != null) v.SetMute(false, IntPtr.Zero); return 0; }
            catch (Exception e) { Console.Error.WriteLine("audio_capture: " + e.Message); return 1; }
        }
        string flag = args.Length >= 3 && args[1] == "--mute" ? args[2] : null;
        uint pid;
        if (args.Length < 1 || !uint.TryParse(args[0], out pid)) { Console.Error.WriteLine("usage: audio_capture <pid>"); return 2; }
        IAudioEndpointVolume spk = null; bool mutedByUs = false;
        try
        {
            if (flag != null)
            {
                try
                {
                    spk = Speakers(); bool was;
                    if (spk != null && spk.GetMute(out was) == 0 && !was)
                    {
                        File.WriteAllText(flag, "muted by audio_capture");
                        mutedByUs = spk.SetMute(true, IntPtr.Zero) == 0;
                        if (mutedByUs) Console.Error.WriteLine("audio_capture: laptop speakers muted");
                        else File.Delete(flag);
                    }
                }
                catch (Exception e) { Console.Error.WriteLine("audio_capture: mute failed: " + e.Message); }
            }
            return Run(pid);
        }
        catch (Exception e) { Console.Error.WriteLine("audio_capture: " + e.Message); return 1; }
        finally
        {
            if (mutedByUs)
            {
                try { spk.SetMute(false, IntPtr.Zero); File.Delete(flag); Console.Error.WriteLine("audio_capture: laptop speakers unmuted"); }
                catch (Exception e) { Console.Error.WriteLine("audio_capture: unmute failed: " + e.Message); }
            }
        }
    }

    static int Run(uint pid)
    {
        var ap = new AUDIOCLIENT_ACTIVATION_PARAMS { ActivationType = 1, TargetProcessId = pid, ProcessLoopbackMode = 0 };
        IntPtr pAp = Marshal.AllocHGlobal(Marshal.SizeOf(ap));
        Marshal.StructureToPtr(ap, pAp, false);
        var pv = new BLOB_PROPVARIANT { vt = 65, cbSize = (uint)Marshal.SizeOf(ap), pBlobData = pAp };
        IntPtr pPv = Marshal.AllocHGlobal(Marshal.SizeOf(pv));
        Marshal.StructureToPtr(pv, pPv, false);

        var iid = typeof(IAudioClient).GUID;
        var h = new Handler();
        IActivateAudioInterfaceAsyncOperation op;
        int hr = ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iid, pPv, h, out op);
        if (hr != 0) { Console.Error.WriteLine("ActivateAudioInterfaceAsync 0x" + hr.ToString("X8")); return 1; }
        if (!h.Done.WaitOne(5000) || h.Hr != 0) { Console.Error.WriteLine("activation failed 0x" + h.Hr.ToString("X8")); return 1; }
        var client = (IAudioClient)h.Iface;

        var fmt = new WAVEFORMATEX { wFormatTag = 1, nChannels = CHANNELS, nSamplesPerSec = SAMPLE_RATE,
            wBitsPerSample = BITS, nBlockAlign = CHANNELS * BITS / 8, nAvgBytesPerSec = SAMPLE_RATE * CHANNELS * BITS / 8 };
        const uint LOOPBACK = 0x00020000, EVENTCALLBACK = 0x00040000, AUTOCONVERTPCM = 0x80000000, SRC_DEFAULT_QUALITY = 0x08000000;
        hr = client.Initialize(0, LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY, 200000, 0, ref fmt, IntPtr.Zero);
        if (hr != 0) { Console.Error.WriteLine("Initialize 0x" + hr.ToString("X8")); return 1; }
        IntPtr ev = CreateEventW(IntPtr.Zero, false, false, IntPtr.Zero);
        client.SetEventHandle(ev);
        var ccIid = typeof(IAudioCaptureClient).GUID;
        object svc;
        hr = client.GetService(ref ccIid, out svc);
        if (hr != 0) { Console.Error.WriteLine("GetService 0x" + hr.ToString("X8")); return 1; }
        var cap = (IAudioCaptureClient)svc;

        var stdout = Console.OpenStandardOutput();
        // stdin closing (the server went away) ends the capture
        var quit = new ManualResetEvent(false);
        new Thread(() => { try { var s = Console.OpenStandardInput(); var b = new byte[64]; while (s.Read(b, 0, b.Length) > 0) { } } catch { } quit.Set(); }) { IsBackground = true }.Start();

        hr = client.Start();
        if (hr != 0) { Console.Error.WriteLine("Start 0x" + hr.ToString("X8")); return 1; }
        Console.Error.WriteLine("audio_capture: capturing pid " + pid);
        byte[] buf = new byte[SAMPLE_RATE * 4];
        int block = CHANNELS * BITS / 8;
        while (!quit.WaitOne(0))
        {
            WaitForSingleObject(ev, 100);
            uint n;
            while (cap.GetNextPacketSize(out n) == 0 && n > 0)
            {
                IntPtr data; uint frames, flags; ulong dp, qp;
                if (cap.GetBuffer(out data, out frames, out flags, out dp, out qp) != 0) break;
                int bytes = (int)frames * block;
                if (bytes > buf.Length) buf = new byte[bytes];
                if ((flags & 0x2) != 0) Array.Clear(buf, 0, bytes);      // AUDCLNT_BUFFERFLAGS_SILENT
                else Marshal.Copy(data, buf, 0, bytes);
                cap.ReleaseBuffer(frames);
                try { stdout.Write(buf, 0, bytes); stdout.Flush(); }
                catch (IOException) { return 0; }                       // the reader went away
            }
        }
        client.Stop();
        return 0;
    }
}
