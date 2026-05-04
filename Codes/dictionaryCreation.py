import json
import os


UnitDict = {'T' : "[degC]", 'i0ref_neg' : "[A/m^2]", 'kappa_film': "[S/m]", 'alpha' : "",
        'J': "", 'f': "[1/s]", 'H': "", 'i1C_loc' : "",
        'cdl_neg' : "[F/m^2]", 'rp_pos'   : "[m]", 'brugl_pos' : "", 
        'i0ref_pos' : "[A/m^2]", 'dn' : "" , 'kappa_film_C': "[S/m]" , 
        'alpha_C' : "", 'J_C' : "", 'f_C' : "[1/s]" , 'H_C' : "" ,
        'i1C_loc_C' : " [A/m^2]", 'cdlvol_cs_pos' : "[F/m^3]" , 'cdl_pos' : "[F/m^2]" , 
        'EC' : "", 'Objective' : ""}
Dict = {'T' : "45[degC]", 'i0ref_neg' : "0.96[A/m^2]", 'kappa_film': "5e-6[S/m]", 'alpha' : "0.67",
         'J': "8.40E-04", 'f': "2.0e2[1/s]", 'H': "6.7", 'i1C_loc' : "1.1687",
        'cdl_neg' : "0.2393[F/m^2]", 'rp_pos' : "2.96e-6[m]", 'brugl_pos' : "1.81578237", 
        'i0ref_pos' : "36.61[A/m^2]", 'dn' : "1" , 'kappa_film_C': "5e-6[S/m]" , 
        'alpha_C' : "0.67", 'J_C' : "8.40E-04", 'f_C' : "2.0e2[1/s]" , 'H_C' : "6.7" ,
        'i1C_loc_C' : "1.0155 [A/m^2]", 'cdlvol_cs_pos' : "2.577e5[F/m^3]" , 'cdl_pos' : "0.2393[F/m^2]" , 
        'EC' : "1", 'Objective' : ""}

file_path = os.getcwd()
loc1 = file_path + '\\DictionData.txt'
loc2 = file_path + '\\Units.txt'

with open(loc1, 'w') as file:
    file.write(json.dumps(Dict, indent = 4))

with open(loc2, 'w') as file:
    file.write(json.dumps(UnitDict, indent = 4))

