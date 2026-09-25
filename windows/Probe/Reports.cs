using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;

namespace Probe1401
{
    class ReportFile
    {
        public string Zip, Summary, Drive;
        public DateTime When;
        public long Bytes;

        public override string ToString()
        {
            return string.Format("{0}   {1:yyyy-MM-dd HH:mm}   {2:0.0} MB   ({3})", Path.GetFileNameWithoutExtension(Zip),
                When, Bytes / 1048576.0, Drive);
        }
    }

    /// <summary>Finds the reports the scanner left on any PROBE1401 stick. This app never uploads anything:
    /// it shows the file and opens the upload page the tester was given; the tester does the upload.</summary>
    static class Reports
    {
        public static List<ReportFile> Find()
        {
            var found = new List<ReportFile>();
            foreach (var d in DriveInfo.GetDrives())
            {
                try
                {
                    if (!d.IsReady || !string.Equals(d.VolumeLabel, Disks.Label, StringComparison.OrdinalIgnoreCase)) continue;
                    string dir = Path.Combine(d.RootDirectory.FullName, "REPORTS");
                    if (!Directory.Exists(dir)) continue;
                    foreach (var z in Directory.GetFiles(dir, "*.zip"))
                    {
                        var fi = new FileInfo(z);
                        string txt = Path.ChangeExtension(z, ".txt");
                        found.Add(new ReportFile { Zip = z, Summary = File.Exists(txt) ? txt : null, Drive = d.Name.TrimEnd('\\'),
                                                   When = fi.LastWriteTime, Bytes = fi.Length });
                    }
                }
                catch (Exception) { /* a drive that vanished mid-scan (a card reader) is simply not listed */ }
            }
            return found.OrderByDescending(r => r.When).ToList();
        }

        /// <summary>The upload link, from probe.json next to the exe. Empty when none was configured.</summary>
        public static string UploadUrl()
        {
            try
            {
                string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "probe.json");
                if (!File.Exists(p)) return "";
                var cfg = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(p));
                object u;
                string url = cfg != null && cfg.TryGetValue("upload_url", out u) ? ("" + u).Trim() : "";
                // only a web link is ever opened, never a file:, javascript: or program path
                return url.StartsWith("https://", StringComparison.OrdinalIgnoreCase) ? url : "";
            }
            catch (Exception) { return ""; }
        }
    }
}
