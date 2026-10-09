using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

namespace Probe1401
{
    /// <summary>The whole app is one wizard window:
    ///   Home -> 1 Check this PC -> 2 Pick the stick -> 3 Make it -> 4 Start the scanner
    ///   Home -> Find my report (after the scan)</summary>
    class MainForm : Form
    {
        readonly Label title = new Label(), step = new Label();
        readonly FlowLayoutPanel body = new FlowLayoutPanel();
        readonly Button back = new Button(), next = new Button(), close = new Button();
        readonly System.Windows.Forms.Timer usbTimer = new System.Windows.Forms.Timer { Interval = 2000 };
        readonly string payload = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "payload");
        SystemInfo sys;
        UsbDisk chosen;
        Action onBack, onNext;
        bool busy;

        public MainForm()
        {
            Text = "1401 Probe - NullMoth Systems";
            Font = Theme.Body;
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96f, 96f);
            ClientSize = new Size(780, 600);
            MinimumSize = new Size(640, 520);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Theme.Panel;
            ForeColor = Theme.Text;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch (ArgumentException) { }  // no icon is cosmetic; the window still opens

            var grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(24, 16, 24, 12) };
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            var head = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, AutoSize = true, WrapContents = false, Dock = DockStyle.Fill };
            // the site's banner: moth mark + NULLMOTH SYSTEMS in a ridged purple box, the page title under a groove
            var banner = new FlowLayoutPanel { AutoSize = true, WrapContents = false, BackColor = Theme.Panel2,
                                               Padding = new Padding(8), Margin = new Padding(0, 0, 0, 10) };
            var mark = Theme.Mark();
            if (mark != null)
                banner.Controls.Add(new PictureBox { Image = mark, SizeMode = PictureBoxSizeMode.Zoom, Size = new Size(56, 56),
                                                     Margin = new Padding(0, 0, 12, 0) });
            var names = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, AutoSize = true, WrapContents = false };
            names.Controls.Add(new Label { Text = "NULLMOTH SYSTEMS", Font = Theme.Brand, ForeColor = Theme.Purple, AutoSize = true });
            names.Controls.Add(new Label { Text = "1401 PROBE · HARDWARE SCANNER", Font = Theme.Mono, ForeColor = Theme.Cyan, AutoSize = true });
            banner.Controls.Add(names);
            head.Controls.Add(banner);
            title.Font = Theme.Title;
            title.ForeColor = Theme.Text;
            title.AutoSize = true;
            step.AutoSize = true;
            step.Font = Theme.Mono;
            step.ForeColor = Theme.Muted;
            head.Controls.Add(title);
            head.Controls.Add(step);
            body.Dock = DockStyle.Fill;
            body.FlowDirection = FlowDirection.TopDown;
            body.WrapContents = false;
            body.AutoScroll = true;
            body.Padding = new Padding(0, 12, 0, 0);
            var buttons = new FlowLayoutPanel { FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Dock = DockStyle.Fill };
            foreach (var b in new[] { close, next, back }) { b.AutoSize = true; b.Padding = new Padding(10, 3, 10, 3); Theme.Style(b, b == next); buttons.Controls.Add(b); }
            close.Text = "Close";
            back.Text = "‹ Back";
            next.Text = "Next ›";
            close.Click += (s, e) => Close();
            back.Click += (s, e) => { if (onBack != null) onBack(); };
            next.Click += (s, e) => { if (onNext != null) onNext(); };
            grid.Controls.Add(head, 0, 0);
            grid.Controls.Add(body, 0, 1);
            grid.Controls.Add(buttons, 0, 2);
            Controls.Add(grid);
            usbTimer.Tick += (s, e) => RefreshUsb();
            FormClosing += (s, e) =>
            {
                if (busy) { e.Cancel = true; MessageBox.Show(this, "Please wait, the stick is still being written.", "1401 Probe"); }
            };
            Shown += (s, e) => Home();
        }

        // ----------------------------------------------------------------------------------- page helpers

        void Page(string t, string s, Action backTo, string nextText, Action nextTo)
        {
            usbTimer.Stop();
            body.Controls.Clear();
            title.Text = t;
            step.Text = s;
            onBack = backTo;
            onNext = nextTo;
            back.Visible = backTo != null;
            next.Visible = nextTo != null;
            next.Enabled = true;
            back.Enabled = true;
            next.Text = nextText ?? "Next ›";
        }

        Label Text_(string text, bool bold = false, Color? color = null)
        {
            var l = new Label
            {
                Text = text, AutoSize = true, Margin = new Padding(0, 0, 0, 10),
                MaximumSize = new Size(body.ClientSize.Width - 30, 0)
            };
            if (bold) l.Font = new Font(Font, FontStyle.Bold);
            if (color.HasValue) l.ForeColor = color.Value;
            body.Controls.Add(l);
            return l;
        }

        Button Btn(string text, EventHandler click)
        {
            var b = new Button { Text = text, AutoSize = true, Padding = new Padding(10, 4, 10, 4), Margin = new Padding(0, 0, 0, 10) };
            Theme.Style(b);
            b.Click += click;
            body.Controls.Add(b);
            return b;
        }

        static readonly Color Good = Theme.Cyan, Warn = Theme.Amber, Bad = Theme.Err;
        static readonly string Docs = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "docs");

        void OpenDoc(string name)
        {
            string p = Path.Combine(Docs, name);
            if (File.Exists(p)) Process.Start(p);
            else MessageBox.Show(this, "Missing file: " + p + "\r\nReinstall 1401 Probe.", "1401 Probe");
        }

        // ----------------------------------------------------------------------------------- pages

        void Home()
        {
            Page("1401 Probe", "FREE FOREVER · OFFLINE · READ-ONLY", null, null, null);
            Text_("This app makes a USB stick that scans your computer's hardware (processor, motherboard, " +
                  "graphics card, Wi-Fi and Bluetooth chips, sound, USB ports and firmware) and saves one report file " +
                  "back to the stick.");
            Text_("The scanner runs on its own, offline. It starts with no network drivers (it asks before it ever loads " +
                  "Wi-Fi, Bluetooth or Ethernet drivers, and never goes online), locks every drive except its own " +
                  "stick read-only, and removes serial numbers before it saves anything. You upload the report yourself " +
                  "at nullmothsystems.com.");
            Text_("You need: a USB stick of 1 GB or more whose contents you don't need, and about 30 minutes.", true);
            Btn("Make the scanner USB stick  ›", (s, e) => Checks());
            Btn("I already scanned - find my report  ›", (s, e) => ReportPage());
            var docs = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0, 6, 0, 0) };
            foreach (var d in new[] { Tuple.Create("How to use it", "instructions.html"), Tuple.Create("License", "LICENSE.txt"),
                                      Tuple.Create("Open-source notices", "THIRD-PARTY-NOTICES.txt") })
            {
                var b = new Button { Text = d.Item1, AutoSize = true, Padding = new Padding(6, 2, 6, 2), Margin = new Padding(0, 0, 8, 0) };
                Theme.Style(b, false);
                string f = d.Item2;
                b.Click += (s, e) => OpenDoc(f);
                docs.Controls.Add(b);
            }
            body.Controls.Add(docs);
        }

        void Checks()
        {
            Page("Check this PC", "Step 1 of 4", Home, null, PickStick);
            Text_("Reading this PC's firmware settings...");
            Cursor = Cursors.WaitCursor;
            try { sys = SystemChecks.Read(); } finally { Cursor = Cursors.Default; }
            body.Controls.Clear();
            string who = (sys.Maker + " " + sys.Model).Trim();
            if (sys.BoardName.Length > 0) who += "  (board: " + sys.BoardMaker + " " + sys.BoardName + ")";
            Text_("This computer: " + who);

            if (sys.Firmware == "UEFI") Text_("Starts in UEFI mode: good.", false, Good);
            else if (sys.Firmware == "BIOS")
                Text_("Windows started in legacy BIOS mode. The scanner needs UEFI. You can still make the stick; " +
                      "in your BIOS setup, set Boot Mode to UEFI (turn CSM/Legacy off) before starting it.", false, Warn);
            else Text_("?  Could not tell whether this PC starts in UEFI mode (" + sys.Firmware + ").", false, Warn);

            if (sys.SecureBoot == "off") Text_("Secure Boot is off: good.", false, Good);
            else if (sys.SecureBoot == "on")
            {
                string err = Restore.Mark("secureboot-was-on");
                Text_("Secure Boot is ON. Before starting the scanner, turn Secure Boot OFF in your BIOS setup " +
                      "(the scanner is not signed by Microsoft). When you come back for your report, this app reminds " +
                      "you to turn it back on." + (err == null ? "" : " (Could not save that reminder: " + err + ")"), false, Warn);
            }
            else Text_("?  Could not read Secure Boot (" + sys.SecureBoot + "). If the stick won't start, turn Secure Boot off.", false, Warn);

            if (sys.BitLocker == "on")
            {
                Text_("BitLocker protects this PC. Changing Secure Boot makes Windows ask for your BitLocker recovery " +
                      "key on the next start. Either have the key ready (aka.ms/myrecoverykey), or pause BitLocker for " +
                      "the next 3 restarts, then it turns itself back on.", false, Bad);
                Btn("Pause BitLocker for 3 restarts", (s, e) => PauseBitLocker());
            }
            else if (sys.BitLocker == "off") Text_("BitLocker is not protecting the Windows drive.", false, Good);
            else Text_("?  Could not read BitLocker (" + sys.BitLocker + "). Have your recovery key ready to be safe.", false, Warn);
        }

        void PauseBitLocker()
        {
            string drive = Environment.GetEnvironmentVariable("SystemDrive") ?? "C:";
            if (MessageBox.Show(this, "Pause BitLocker on " + drive + " for the next 3 restarts?\r\n\r\nIt resumes by itself after that.",
                    "1401 Probe", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            var r = SystemChecks.Run(SystemChecks.Sys32("manage-bde.exe"), "-protectors -disable " + drive + " -RebootCount 3");
            if (r.Item1 == 0) Restore.Mark("bitlocker-paused");
            MessageBox.Show(this, r.Item1 == 0 ? "BitLocker is paused for 3 restarts." : "manage-bde failed:\r\n" + r.Item2,
                "1401 Probe", MessageBoxButtons.OK, r.Item1 == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Error);
        }

        ListBox usbList;
        CheckBox understand;

        void PickStick()
        {
            Page("Plug in a USB stick", "Step 2 of 4", Checks, "Erase and make the stick ›", MakeStick);
            Text_("Plug in the USB stick you want to use. It appears below within a few seconds.");
            Text_("EVERYTHING on the stick you pick will be erased.", true, Bad);
            usbList = new ListBox { Width = body.ClientSize.Width - 30, Height = 150, Margin = new Padding(0, 0, 0, 10), IntegralHeight = false };
            Theme.Style(usbList);
            body.Controls.Add(usbList);
            understand = new CheckBox { Text = "I understand everything on this stick will be erased.", AutoSize = true };
            body.Controls.Add(understand);
            Text_("Only USB drives are listed. Your internal drives and the Windows drive are never shown.", false, Theme.Muted);
            usbList.SelectedIndexChanged += (s, e) => UpdatePick();
            understand.CheckedChanged += (s, e) => UpdatePick();
            RefreshUsb();
            usbTimer.Start();
            UpdatePick();
        }

        void UpdatePick()
        {
            var d = usbList == null ? null : usbList.SelectedItem as UsbDisk;
            next.Enabled = d != null && understand.Checked && d.Size >= 900000000UL;
        }

        void RefreshUsb()
        {
            if (usbList == null || usbList.IsDisposed) return;
            List<UsbDisk> disks;
            try { disks = Disks.ListUsb(); }
            catch (Exception e) { usbTimer.Stop(); MessageBox.Show(this, "Could not list USB drives: " + e.Message, "1401 Probe"); return; }
            var sel = usbList.SelectedItem as UsbDisk;
            string now = string.Join("|", disks.Select(d => d.UniqueId + d.Size + d.Letters));
            string was = string.Join("|", usbList.Items.Cast<UsbDisk>().Select(d => d.UniqueId + d.Size + d.Letters));
            if (now == was) return;
            usbList.BeginUpdate();
            usbList.Items.Clear();
            foreach (var d in disks) usbList.Items.Add(d);
            if (sel != null) usbList.SelectedItem = disks.FirstOrDefault(d => d.UniqueId == sel.UniqueId);
            else if (disks.Count == 1) usbList.SelectedIndex = 0;
            usbList.EndUpdate();
            UpdatePick();
        }

        ProgressBar bar;
        Label status;

        void MakeStick()
        {
            chosen = usbList.SelectedItem as UsbDisk;
            if (chosen == null) return;
            if (MessageBox.Show(this, "Erase this drive and make it the 1401 Probe stick?\r\n\r\n" + chosen +
                    "\r\n\r\nEverything on it will be lost. This cannot be undone.", "1401 Probe: erase the stick",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
            // a USB drive this big is far more likely a backup or external SSD than a spare stick: ask a second time
            if (chosen.Size > 130000000000UL && MessageBox.Show(this, "This drive is " + (chosen.Size / 1e9).ToString("0") +
                    " GB, bigger than a normal USB stick.\r\n\r\nIf it is an external hard drive, an SSD, or a backup drive, click No and " +
                    "use a small, empty USB stick instead.\r\n\r\nErase it anyway?", "1401 Probe: large drive",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
            Page("Making the stick", "Step 3 of 4", null, null, null);
            Text_("Don't unplug the stick.");
            bar = new ProgressBar { Width = body.ClientSize.Width - 30, Height = 22, Margin = new Padding(0, 0, 0, 10) };
            body.Controls.Add(bar);
            status = Text_("Starting...");
            busy = true;
            var disk = chosen;
            new Thread(() =>
            {
                string err = null;
                try { Disks.MakeProbeStick(disk, payload, (p, t) => BeginInvoke((Action)(() => { bar.Value = Math.Min(100, p); status.Text = t; }))); }
                catch (Exception e) { err = e.Message; }
                BeginInvoke((Action)(() =>
                {
                    busy = false;
                    if (err == null) BootHowTo();
                    else
                    {
                        Page("The stick was not made", "Step 3 of 4", PickStick, null, null);
                        Text_(err, false, Bad);
                        Text_("Try again, or try a different USB stick.");
                    }
                }));
            }) { IsBackground = true }.Start();
        }

        void BootHowTo()
        {
            if (sys == null) sys = SystemChecks.Read();
            Page("Start the scanner", "Step 4 of 4", null, null, null);
            Text_("The stick is ready and every file was checked.", false, Good);
            if (sys.SecureBoot == "on")
                Text_("1.  Turn Secure Boot OFF in your BIOS setup first (use \"Restart into BIOS setup\" below).", true, Warn);
            string key = SystemChecks.BootKey(sys);
            string how = key == null ? "Open your computer's boot menu (usually F12, F11, F8 or Esc as it starts), "
                : key.StartsWith("F") ? "Press " + key + " repeatedly as the computer starts to open its boot menu, "
                : "Open the boot menu (" + key + "), ";
            Text_("2.  Leave the stick plugged in and restart. " + how + "and pick the USB stick marked UEFI.");
            Text_("3.  Pick \"1401 Probe - scan this computer\". It asks once whether to include Wi-Fi, Bluetooth and " +
                  "Ethernet (press Y within 30 seconds, or nothing for no). The scan takes about 5 to 20 minutes. The last " +
                  "steps test the graphics card and the screen may go black for up to 10 minutes. Leave it running. If " +
                  "nothing happens for 15 minutes, hold the power button. It turns the computer off by itself.");
            Text_("4.  Put your settings back: turn Secure Boot back ON in BIOS setup (Del or F2 as it starts), then start " +
                  "Windows, open 1401 Probe, and click \"I already scanned - find my report\".", true);
            Btn("Restart now and pick the USB stick (Use a device)", (s, e) => Restart("/r /o /t 0",
                "Windows will restart into its blue startup menu. Choose \"Use a device\", then your USB stick."));
            if (sys.Firmware == "UEFI")
                Btn("Restart into BIOS setup (to turn Secure Boot off)", (s, e) => Restart("/r /fw /t 0",
                    "The computer will restart straight into its BIOS/UEFI setup."));
            Btn("‹ Back to the start", (s, e) => Home());
        }

        void Restart(string args, string what)
        {
            if (MessageBox.Show(this, what + "\r\n\r\nSave your work in other apps first. Restart now?", "1401 Probe",
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            var r = SystemChecks.Run(SystemChecks.Sys32("shutdown.exe"), args);
            if (r.Item1 != 0) MessageBox.Show(this, "Windows refused to restart that way:\r\n" + r.Item2, "1401 Probe");
        }

        void ReportPage()
        {
            Page("Find my report", "After the scan", Home, null, null);
            var reports = Reports.Find();
            if (reports.Count == 0)
            {
                Text_("No report found yet. Plug in the 1401 stick (the drive named PROBE1401) and click Look again.", false, Warn);
                Btn("Look again", (s, e) => ReportPage());
                return;
            }
            Text_("Found " + reports.Count + " report" + (reports.Count == 1 ? "" : "s") + ":", true);
            var list = new ListBox { Width = body.ClientSize.Width - 30, Height = 110, Margin = new Padding(0, 0, 0, 10), IntegralHeight = false };
            Theme.Style(list);
            foreach (var r in reports) list.Items.Add(r);
            list.SelectedIndex = 0;
            body.Controls.Add(list);
            Func<ReportFile> cur = () => list.SelectedItem as ReportFile;
            Btn("Read the summary", (s, e) => { var r = cur(); if (r != null && r.Summary != null) Process.Start("notepad.exe", "\"" + r.Summary + "\""); });
            SettingsBack();
            string url = Reports.UploadUrl();
            Text_("Upload: open the upload page, then drag BOTH files onto it: the .zip and the .txt with the same name. " +
                  "This app never uploads anything by itself.", true);
            Btn("Show both files", (s, e) => { var r = cur(); if (r != null) Process.Start("explorer.exe", "/select,\"" + r.Zip + "\""); });
            if (url.Length > 0) Btn("Open the upload page (" + new Uri(url).Host + ")", (s, e) => Process.Start(url));
            else Text_("Send both files to the person who gave you 1401 Probe, using the link they gave you.", false, Warn);
            Btn("Copy both to my Desktop", (s, e) =>
            {
                var r = cur();
                if (r == null) return;
                string desk = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                foreach (var f in new[] { r.Zip, r.Summary })
                    if (f != null && File.Exists(f)) File.Copy(f, Path.Combine(desk, Path.GetFileName(f)), true);
                MessageBox.Show(this, "Copied the .zip and .txt to your Desktop.", "1401 Probe");
            });
            Text_("Done with the stick? You can turn it back into an ordinary USB drive. Copy the report to your Desktop first.", false, Theme.Muted);
            Btn("Make it an ordinary USB drive again", (s, e) => GiveBack());
        }

        /// <summary>Put the tester's PC back the way it was. Reads the live state every time; the marks from Checks()
        /// only decide whether to say "it was on before". Boot order is never changed (the boot menu is one-time).</summary>
        void SettingsBack()
        {
            var now = SystemChecks.Read();
            Text_("Put your settings back", true, Theme.Purple);
            if (now.SecureBoot == "on")
            {
                Restore.Clear("secureboot-was-on");
                Text_("Secure Boot is on.", false, Good);
            }
            else if (now.SecureBoot == "off")
            {
                Text_("Secure Boot is OFF. " + (Restore.Has("secureboot-was-on") ? "It was on before you scanned. " :
                      "Most PCs come with it on. ") + "Turn it back on: restart into BIOS setup, set Secure Boot to Enabled, " +
                      "save and exit. (If Windows then asks for a BitLocker key, it's at aka.ms/myrecoverykey.)", false, Warn);
                if (now.Firmware == "UEFI")
                    Btn("Restart into BIOS setup (to turn Secure Boot back on)", (s, e) => Restart("/r /fw /t 0",
                        "The computer will restart straight into its BIOS/UEFI setup. Set Secure Boot to Enabled, then save and exit."));
            }
            else Text_("?  Could not read Secure Boot (" + now.SecureBoot + "). If you turned it off, turn it back on in BIOS setup.", false, Warn);

            if (Restore.Has("bitlocker-paused") && now.BitLocker == "on")
            {
                Restore.Clear("bitlocker-paused");
                Text_("BitLocker is protecting this PC again.", false, Good);
            }
            else if (Restore.Has("bitlocker-paused"))
            {
                Text_("BitLocker is still paused (it resumes by itself after 3 restarts). You can turn it back on now.", false, Warn);
                Btn("Turn BitLocker back on now", (s, e) => ResumeBitLocker());
            }
            Text_("Your boot order was not changed. The boot menu is a one-time choice.", false, Theme.Muted);
        }

        void ResumeBitLocker()
        {
            // turn Secure Boot back on first: resuming protection while it is still off would seal BitLocker to the
            //   off state, and the next start with Secure Boot on would ask for the recovery key
            var now = SystemChecks.Read();
            if (now.SecureBoot != "on" && MessageBox.Show(this, "Secure Boot is still off. Turn it back on first, then resume " +
                    "BitLocker, otherwise Windows may ask for your recovery key.\r\n\r\nResume BitLocker anyway?", "1401 Probe",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
            string drive = Environment.GetEnvironmentVariable("SystemDrive") ?? "C:";
            var r = SystemChecks.Run(SystemChecks.Sys32("manage-bde.exe"), "-protectors -enable " + drive);
            if (r.Item1 == 0) Restore.Clear("bitlocker-paused");
            MessageBox.Show(this, r.Item1 == 0 ? "BitLocker is protecting this PC again." : "manage-bde failed:\r\n" + r.Item2,
                "1401 Probe", MessageBoxButtons.OK, r.Item1 == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Error);
            ReportPage();
        }

        void GiveBack()
        {
            var stick = Disks.ListUsb().FirstOrDefault(d => Reports.Find().Any(r => d.Letters.Contains(r.Drive)));
            if (stick == null) { MessageBox.Show(this, "Could not find which disk the PROBE1401 drive is on.", "1401 Probe"); return; }
            if (MessageBox.Show(this, "Erase the 1401 stick and make it an ordinary USB drive?\r\n\r\n" + stick +
                    "\r\n\r\nThe report on it will be deleted.", "1401 Probe", MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
                    MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
            try
            {
                Cursor = Cursors.WaitCursor;
                Disks.Reformat(stick, (p, t) => { });
                MessageBox.Show(this, "Done. The stick is an ordinary USB drive again.", "1401 Probe");
            }
            catch (Exception e) { MessageBox.Show(this, e.Message, "1401 Probe", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            finally { Cursor = Cursors.Default; }
            Home();
        }
    }
}
