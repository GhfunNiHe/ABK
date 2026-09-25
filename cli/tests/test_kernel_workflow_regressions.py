import importlib.util
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "build.yml"
PREFLIGHT_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "kernel-source.yml"
REF_SCRIPT_PATH = ROOT / ".github" / "scripts" / "resolve-ksu-ref.sh"
KSU_COMPAT_PATH = ROOT / ".github" / "scripts" / "ensure-ksu-compat.py"
SALVAGE_SCRIPT_PATH = ROOT / ".github" / "scripts" / "salvage-patch-rejects.py"


class KernelWorkflowRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.preflight_workflow = PREFLIGHT_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.ref_script = REF_SCRIPT_PATH.read_text(encoding="utf-8")
        spec = importlib.util.spec_from_file_location("ensure_ksu_compat", KSU_COMPAT_PATH)
        cls.ksu_compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ksu_compat)
        salvage_spec = importlib.util.spec_from_file_location("salvage_patch_rejects", SALVAGE_SCRIPT_PATH)
        cls.salvage = importlib.util.module_from_spec(salvage_spec)
        salvage_spec.loader.exec_module(cls.salvage)

    def test_sukisu_setup_accepts_resolved_bare_sha(self):
        block = self._step_run_block("添加 KernelSU")
        case = block.split('"SukiSU")', 1)[1].split('"ReSukiSU")', 1)[0]

        self.assertIn('requested_ref="$BRANCH"', case)
        self.assertNotIn('${BRANCH#-s }', case)
        self.assertIn('bash "$setup_script" "$requested_ref"', case)
        self.assertIn('requested_head="$(git -C KernelSU rev-parse', case)

    def test_development_refs_are_reachable_successful_main_builds(self):
        expected = {
            "OFFICIAL_DEV_REF": "33d0c9205df47b6b1b61c25c13afa164b88871d1",
            "SUKISU_DEV_REF": "9fbe8fe8ca90c62c259c5894bf96d02ac31209b9",
            "RESUKISU_DEV_REF": "246d3e52e667cb72ce8f70c93b70d3b42b100b76",
        }
        for variable, sha in expected.items():
            with self.subTest(variable=variable):
                self.assertRegex(
                    self.ref_script,
                    rf'(?m)^{variable}="{sha}"$',
                )

    def test_resolved_sha_defaults_to_selected_variant_ref(self):
        self.assertIn(
            'emit_env "RESOLVED_KSU_SHA" "${RESOLVED_KSU_SHA:-$BRANCH}"',
            self.ref_script,
        )

    def test_susfs_compatibility_step_covers_sukisu_variants(self):
        step = self.workflow.split("- name: 确保 KernelSU SUSFS ABI 兼容", 1)[1].split("- name: 配置 SukiSU 管理器信息", 1)[0]
        self.assertIn(
            "if: (inputs.ksu_variant == 'SukiSU' || inputs.ksu_variant == 'ReSukiSU') && inputs.enable_susfs",
            step,
        )
        self.assertIn("custom-source-feature-env.sh", step)
        self.assertIn("ABK_FEATURE_ID: kernelsu", step)

    def test_sukisu_nested_supercall_gets_susfs_fd_compatibility(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ksu = root / "KernelSU" / "kernel"
            supercall = ksu / "supercall"
            feature = ksu / "feature"
            supercall.mkdir(parents=True)
            feature.mkdir()
            (ksu / "Kbuild").write_text("obj-y += supercall/\n", encoding="utf-8")
            source = supercall / "supercall.c"
            header = supercall / "supercall.h"
            source.write_text(
                "int ksu_install_fd(void) { return 0; }\n"
                "void __init ksu_supercalls_init(void) {}\n",
                encoding="utf-8",
            )
            header.write_text("int ksu_install_fd(void);\n", encoding="utf-8")
            (feature / "kernel_umount.c").write_text(
                "int ksu_kernel_umount_enabled;\n"
                "\nstatic const struct ksu_feature_handler kernel_umount_handler = {};\n",
                encoding="utf-8",
            )

            self.ksu_compat.main(str(root))

            self.assertIn("int ksu_install_su_fd(void)", source.read_text(encoding="utf-8"))
            self.assertIn("int ksu_install_su_fd(void);", header.read_text(encoding="utf-8"))
            self.assertIn(
                "static int kernel_umount_feature_set(u64 value)",
                (feature / "kernel_umount.c").read_text(encoding="utf-8"),
            )

    def test_scoped_su_fd_compatibility_preserves_session_permission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ksu = Path(temp_dir)
            source = ksu / "supercall.c"
            source.write_text(
                "#define KSU_DRIVER_PERMISSION_SU_SESSION (1UL << 0)\n"
                "static int ksu_install_fd_with_permissions(unsigned int flags, unsigned long permissions) { return 0; }\n"
                "int ksu_install_fd(void) { return ksu_install_fd_with_permissions(O_CLOEXEC, 0); }\n"
                "void __init ksu_supercalls_init(void) {}\n",
                encoding="utf-8",
            )

            self.ksu_compat.ensure_su_fd(ksu)

            self.assertIn(
                "ksu_install_fd_with_permissions(O_CLOEXEC, KSU_DRIVER_PERMISSION_SU_SESSION)",
                source.read_text(encoding="utf-8"),
            )

    def test_android12_ntsync_compat_filters_incompatible_lockdep_hunk(self):
        block = self._step_run_block("应用 NTsync 补丁")
        self.assertIn("apply_android12_ntsync_compat", block)
        self.assertIn('source.find("@@ -305,25 +310,29")', block)
        self.assertIn('source.find("@@ -5308,13 +5309,13")', block)
        self.assertIn("#define lockdep_assert(cond)", block)
        self.assertIn("return LOCK_STATE_HELD;", block)
        self.assertIn(
            'if [[ "$ABK_ANDROID_VERSION" == "android12" && "$ABK_KERNEL_VERSION" == "5.10" ]]',
            block,
        )

    def test_android12_statfs_repair_injects_verified_declaration(self):
        block = self._step_run_block("应用 SUSFS 补丁")
        self.assertIn(
            'declaration = "extern int susfs_sus_kstat_spoof_vfs_statfs(struct inode *inode, '
            'struct kstatfs *buf, bool *is_fuse);"',
            block,
        )
        self.assertIn("declared > call", block)
        self.assertIn('#include <linux/security.h>', block)
        self.assertIn('security_sb_statfs(', block)
        self.assertNotIn(
            "android12-5.10 Official fs/statfs.c 缺少 susfs_def.h",
            block,
        )
        statfs_repair = block.split("# Android 12/5.10 的上游补丁", 1)[1].split("# Android 13 - 5.15 修复", 1)[0]
        self.assertNotIn('[[ "$ABK_KSU_VARIANT" == "Official" ]]', statfs_repair)
        self.assertIn("fix_fdinfo_declarations", block)
        self.assertIn('declarations.append("\\tstruct mount *mnt;\\n")', block)

    def test_sukisu_post_exec_wrapper_installs_su_session_fd(self):
        block = self._step_run_block("最终修复 SukiSU/ReSukiSU 源码兼容")
        self.assertIn("ensure_post_execveat_wrapper", block)
        self.assertIn('#include "supercall/supercall.h"', block)
        self.assertIn("int ksu_handle_post_execveat_sucompat(", block)
        self.assertIn("(void)ksu_install_su_fd();", block)

    def test_official_post_exec_wrapper_is_preserved_after_rewrite(self):
        block = self._step_run_block("修复 Official SUSFS 源码兼容")
        self.assertIn('int ksu_handle_post_execveat_sucompat(', block)
        self.assertIn('(void)ksu_install_su_fd();', block)
        self.assertIn('#include "supercall/supercall.h"', block)

    def test_susfs_common_file_fallbacks_cover_upstream_api_renames(self):
        block = self._step_run_block("应用 SUSFS 补丁")
        self.assertIn('mnt_userns', block)
        self.assertIn('text.replace("mnt_userns", "idmap")', block)
        self.assertIn('ensure_susfs_super_compat', block)
        self.assertIn('DEFAULT_KSU_MNT_MINOR_DEV', block)
        self.assertIn('susfs_get_non_sus_mnt_id_unique_from_mnt', block)

    def test_stat_api_rewrite_is_gated_to_native_idmap_kernels(self):
        block = self._step_run_block("应用 SUSFS 补丁")
        self.assertIn("native_new_api = \"struct mnt_idmap\" in text", block)
        self.assertIn(
            'if "mnt_userns" in text and native_new_api:',
            block,
        )

    def test_namespace_tail_repair_closes_truncated_susfs_hunk(self):
        block = self._step_run_block("应用 SUSFS 补丁")
        self.assertIn("ensure_susfs_namespace_tail", block)
        self.assertIn(
            'if (!mnt->mnt.mnt_root || IS_ERR(mnt->mnt.mnt_root)) {',
            block,
        )
        self.assertIn("#endif // #ifdef CONFIG_KSU_SUSFS", block)

    def test_susfs_step_salvages_rejected_addition_hunks(self):
        block = self._step_run_block("应用 SUSFS 补丁")
        self.assertIn("salvage-patch-rejects.py", block)
        self.assertIn('--root "$KERNEL_ROOT/common"', block)
        self.assertIn(
            '--patch "./50_add_susfs_in_gki-${ABK_ANDROID_VERSION}-${ABK_KERNEL_VERSION}.patch"',
            block,
        )

    def test_preflight_queries_custom_source_repo_for_named_refs(self):
        function = self._preflight_function("resolve_named_ref")
        self.assertIn('git -C "$source_dir" ls-remote origin', function)
        self.assertNotRegex(function, r"(?m)^\s*done < <\(git ls-remote origin")
        self.assertIn('"refs/heads/$ref"', function)
        self.assertIn('"refs/tags/$ref"', function)
        self.assertIn('echo "::error::找不到源码 ref: $ref（源码仓库 $normalized_url）" >&2', function)

    def test_preflight_named_ref_resolution_uses_source_dir_not_workspace_repo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_repo = root / "source"
            upstream_repo = root / "upstream"
            workspace_repo = root / "workspace"

            self._init_repo(source_repo, branch="main", content="VERSION = 5\n")
            subprocess.run(
                ["git", "-C", str(source_repo), "branch", "melt-rebase"],
                check=True,
                capture_output=True,
            )
            # 预检会先给 source_dir 配上 origin，再按分支名解析。
            subprocess.run(
                ["git", "-C", str(source_repo), "remote", "add", "origin", str(source_repo)],
                check=True,
                capture_output=True,
            )
            # 模拟 ABK 检出：自己的 origin 指向另一个仓库，且没有 melt-rebase。
            self._init_repo(upstream_repo, branch="dev", content="ABK\n")
            self._init_repo(workspace_repo, branch="dev", content="ABK checkout\n")
            subprocess.run(
                ["git", "-C", str(workspace_repo), "remote", "add", "origin", str(upstream_repo)],
                check=True,
                capture_output=True,
            )

            # 裸 `ls-remote origin` 在工作区仓库里跑，永远找不到自定义源码仓的分支。
            bare = subprocess.run(
                ["git", "ls-remote", "origin", "melt-rebase", "refs/heads/melt-rebase"],
                cwd=workspace_repo,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual("", bare.stdout)

            function = self._preflight_function("resolve_named_ref")
            script = "\n".join(
                [
                    "set -euo pipefail",
                    f'source_dir="{source_repo}"',
                    function,
                    'printf "%s" "$(resolve_named_ref melt-rebase)"',
                ]
            )
            resolved = subprocess.run(
                ["bash", "-c", script],
                cwd=workspace_repo,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual("refs/heads/melt-rebase", resolved.stdout)

    def test_salvage_replays_vendor_include_head_hunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "fs" / "proc" / "task_mmu.c"
            target.parent.mkdir(parents=True)
            target.write_text(
                "#include <linux/shmem_fs.h>\n"
                "#include <linux/uaccess.h>\n"
                "#include <linux/pkeys.h>\n"
                "#include <trace/hooks/mm.h>\n"
                "\n"
                "#include <asm/elf.h>\n",
                encoding="utf-8",
            )
            reject = Path(f"{target}.rej")
            reject.write_text(
                "--- fs/proc/task_mmu.c\n"
                "+++ fs/proc/task_mmu.c\n"
                "@@ -21,6 +21,9 @@\n"
                " #include <linux/shmem_fs.h>\n"
                " #include <linux/uaccess.h>\n"
                " #include <linux/pkeys.h>\n"
                "+#if defined(CONFIG_KSU_SUSFS_SUS_KSTAT) || defined(CONFIG_KSU_SUSFS_SUS_MAP) "
                "|| defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n"
                "+#include <linux/susfs_def.h>\n"
                "+#endif // #if defined(CONFIG_KSU_SUSFS_SUS_KSTAT) || defined(CONFIG_KSU_SUSFS_SUS_MAP) "
                "|| defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n"
                " \n"
                " #include <asm/elf.h>\n",
                encoding="utf-8",
            )
            unrelated = root / "kernel" / "time" / "timer.c.rej"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("--- kernel/time/timer.c\n", encoding="utf-8")

            patch_text = "diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n"
            stats = self.salvage.salvage(root, patch_text)

            self.assertEqual(stats["repaired"], 1)
            self.assertEqual(stats["unresolved"], 0)
            self.assertEqual(stats["ignored"], 1)
            self.assertFalse(reject.exists())
            self.assertTrue(unrelated.exists())
            repatched = target.read_text(encoding="utf-8")
            self.assertIn("#include <linux/susfs_def.h>", repatched)
            self.assertIn("#include <trace/hooks/mm.h>", repatched)
            self.assertLess(
                repatched.index("#include <linux/susfs_def.h>"),
                repatched.index("#include <trace/hooks/mm.h>"),
            )

    def test_salvage_keeps_rejected_hunks_that_remove_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "fs" / "namespace.c"
            target.parent.mkdir(parents=True)
            original = "#include <linux/mnt_idmapping.h>\nold_line();\n"
            target.write_text(original, encoding="utf-8")
            reject = Path(f"{target}.rej")
            reject.write_text(
                "--- fs/namespace.c\n"
                "+++ fs/namespace.c\n"
                "@@ -1,2 +1,2 @@\n"
                " #include <linux/mnt_idmapping.h>\n"
                "-old_line();\n"
                "+new_line();\n",
                encoding="utf-8",
            )

            stats = self.salvage.salvage(
                root,
                "diff --git a/fs/namespace.c b/fs/namespace.c\n",
            )

            self.assertEqual(stats["repaired"], 0)
            self.assertEqual(stats["unresolved"], 1)
            self.assertTrue(reject.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_salvage_keeps_rejects_with_ambiguous_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "fs" / "super.c"
            target.parent.mkdir(parents=True)
            original = "#include <linux/fs.h>\n\n#include <linux/fs.h>\n"
            target.write_text(original, encoding="utf-8")
            reject = Path(f"{target}.rej")
            reject.write_text(
                "--- fs/super.c\n"
                "+++ fs/super.c\n"
                "@@ -1,1 +1,2 @@\n"
                " #include <linux/fs.h>\n"
                "+#include <linux/susfs_def.h>\n",
                encoding="utf-8",
            )

            stats = self.salvage.salvage(
                root,
                "diff --git a/fs/super.c b/fs/super.c\n",
            )

            self.assertEqual(stats["repaired"], 0)
            self.assertEqual(stats["unresolved"], 1)
            self.assertTrue(reject.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_android16_uses_native_ntsync_source(self):
        block = self._step_run_block("应用 NTsync 补丁")
        self.assertIn('if [[ "$ABK_ANDROID_VERSION" != "android16"', block)
        self.assertIn('NTsync 基础源码已由 android16-6.12 内核提供', block)

    def test_android14_builtin_ntsync_does_not_require_symbol_export(self):
        block = self._step_run_block("应用 NTsync 补丁")
        self.assertNotIn("EXPORT_SYMBOL", block)
        self.assertNotIn("拒绝启用 CONFIG_NTSYNC", block)
        self.assertIn("ensure_defconfig_value CONFIG_NTSYNC y", block)

    def _step_run_block(self, name):
        match = re.search(
            rf"(?ms)^      - name: {re.escape(name)}\n.*?^        run: \|\n"
            rf"(?P<body>.*?)(?=^      - name: |^    [A-Za-z0-9_-]+:|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, f"step not found: {name}")
        return match.group("body")

    def _preflight_function(self, name):
        match = re.search(
            rf"(?ms)^(?P<indent>[ ]*){re.escape(name)}\(\) \{{\n.*?^(?P=indent)\}}\n",
            self.preflight_workflow,
        )
        self.assertIsNotNone(match, f"function not found in kernel-source.yml: {name}")
        return match.group(0)

    def _init_repo(self, path, branch, content):
        subprocess.run(
            ["git", "init", "-q", "-b", branch, str(path)],
            check=True,
            capture_output=True,
        )
        (path / "README.md").write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(path), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C", str(path),
                "-c", "user.name=ABK Tests",
                "-c", "user.email=tests@example.invalid",
                "-c", "commit.gpgsign=false",
                "commit", "-qm", "init",
            ],
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
