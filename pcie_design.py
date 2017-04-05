#!/usr/bin/env python3
import argparse
import os

from litex.gen import *
from litex.gen.genlib.io import CRG
from litex.gen.genlib.resetsync import AsyncResetSynchronizer
from litex.gen.genlib.misc import timeline
from litex.build.tools import write_to_file

import platform as pcie_injector

from litex.soc.interconnect.csr import *
from litex.soc.interconnect import wishbone

from litex.soc.integration.soc_core import *
from litex.soc.cores.uart.bridge import UARTWishboneBridge
from litex.soc.integration.builder import *

from litepcie.phy.s7pciephy import S7PCIEPHY
from litepcie.core import LitePCIeEndpoint, LitePCIeMSI
from litepcie.frontend.dma import LitePCIeDMA
from litepcie.frontend.wishbone import LitePCIeWishboneBridge

import cpu_interface


class _CRG(Module, AutoCSR):
    def __init__(self, platform):
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_clk125 = ClockDomain("clk125")

        # soft reset generaton
        self._soft_rst = CSR()
        soft_rst = Signal()
        # trigger soft reset 1us after CSR access to terminate
        # Wishbone access when reseting from PCIe
        self.sync += [
            timeline(self._soft_rst.re & self._soft_rst.r, [(125, [soft_rst.eq(1)])]),
        ]

        # sys_clk / sys_rst (from PCIe)
        self.comb += self.cd_sys.clk.eq(self.cd_clk125.clk)
        self.specials += AsyncResetSynchronizer(self.cd_sys, self.cd_clk125.rst | soft_rst)


class PCIeDMASoC(SoCCore):
    csr_map = {
        "crg":      16,
        "pcie_phy": 17,
        "dma":      18,
        "msi":      19
    }
    csr_map.update(SoCCore.csr_map)
    interrupt_map = {
        "dma_writer": 0,
        "dma_reader": 1
    }
    interrupt_map.update(SoCCore.interrupt_map)
    mem_map = SoCCore.mem_map
    mem_map["csr"] = 0x00000000

    def __init__(self, platform, with_uart_bridge=True):
        clk_freq = 125*1000000
        SoCCore.__init__(self, platform, clk_freq,
            cpu_type=None,
            shadow_base=0x00000000,
            csr_data_width=32,
            with_uart=False,
            ident="LitePCIe example design",
            with_timer=False
        )
        self.submodules.crg = _CRG(platform)

        # PCIe endpoint
        self.submodules.pcie_phy = S7PCIEPHY(platform, link_width=2)
        self.submodules.pcie_endpoint = LitePCIeEndpoint(self.pcie_phy, with_reordering=True)

        # PCIe Wishbone bridge
        self.add_cpu_or_bridge(LitePCIeWishboneBridge(self.pcie_endpoint, lambda a: 1))
        self.add_wb_master(self.cpu_or_bridge.wishbone)

        # PCIe DMA
        self.submodules.dma = LitePCIeDMA(self.pcie_phy, self.pcie_endpoint, with_buffering=True, with_loopback=True)
        self.dma.source.connect(self.dma.sink)

        if with_uart_bridge:
            self.submodules.uart_bridge = UARTWishboneBridge(platform.request("serial"), clk_freq, baudrate=115200)
            self.add_wb_master(self.uart_bridge.wishbone)

        # MSI
        self.submodules.msi = LitePCIeMSI()
        self.comb += self.msi.source.connect(self.pcie_phy.interrupt)
        self.interrupts = {
            "dma_writer":    self.dma.writer.irq,
            "dma_reader":    self.dma.reader.irq
        }
        for k, v in sorted(self.interrupts.items()):
            self.comb += self.msi.irqs[self.interrupt_map[k]].eq(v)

        # led blink
        counter = Signal(32)
        self.sync += counter.eq(counter + 1)
        self.comb += [
            platform.request("user_led", 0).eq(counter[26] & platform.request("user_btn", 0)),
            platform.request("user_led", 1).eq(counter[27] & platform.request("user_btn", 1))
        ]


def main():
    parser = argparse.ArgumentParser(description="PCIe Injector LiteX SoC")
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()

    platform = pcie_injector.Platform()
    soc = PCIeDMASoC(platform, **soc_core_argdict(args))
    builder = Builder(soc, output_dir="build", csr_csv="test/csr.csv")
    vns = builder.build()

    csr_header = cpu_interface.get_csr_header(soc.get_csr_regions(), soc.get_constants())
    write_to_file(os.path.join("software", "pcie", "kernel", "csr.h"), csr_header)

if __name__ == "__main__":
    main()