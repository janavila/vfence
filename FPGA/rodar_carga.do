# Executar em C:/Users/leobo/OneDrive/Desktop/vfence/FPGA
catch {quit -sim}
if {[file exists work]} {vdel -lib work -all}
vlib work
vmap work work
vlog vfence_controle.v vfence_operativa.v vfence_top.v tb_vfence_carga.v
vsim -t 1ps work.tb_vfence_carga
run -all
