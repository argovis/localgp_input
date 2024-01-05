# usage: python wod2mat.py <WOD ascii file directory> <year> <month> <pressure level of interest OR pressure range shallow,deep for integral> <'conservative' or 'potential' temperature switch>

import numpy, pandas, glob, datetime, scipy.io, sys, bisect, gsw, argparse
from wodpy import wod
import scipy.interpolate
import scipy.integrate
from helpers import helpers

pandas.set_option('display.max_colwidth', None)
pandas.set_option('display.max_rows', None)

# argument setup 
parser = argparse.ArgumentParser()
parser.add_argument("--data_dir", type=str, help="directory with ASCII WOD data")
parser.add_argument("--out_dir", type=str, help="directory to write output mat files to")
parser.add_argument("--year", type=int, help="year")
parser.add_argument("--month", type=int, help="month")
parser.add_argument("--pressure", type=float, nargs='+', help="either one pressure level, or a range low high for integration")
parser.add_argument("--temp_type", type=str, help="potential or conservative")
parser.add_argument("--pressure_buffer", type=float, nargs='?', const=100.0, default=100.0, help="pressure range to keep on either side of the pressure ROI")
parser.add_argument("--pressure_index_buffer", type=int, nargs='?', const=5, default=5, help="minimum number of elements to preserve in the pressure buffer margins")
args = parser.parse_args()

#files = glob.glob("/scratch/alpine/wimi7695/wod/all/ocldb*")

# ingest command line arguments
files = glob.glob(args.data_dir + '/ocldb*')
#file = sys.argv[1]
y = int(args.year)
m = int(args.month)
p_arg = args.pressure
p_interp = False
p_range = False
if len(p_arg) == 1:
	# single level interpolation
	p_interp = p_arg[0]
else:
	# level integral
	p_range = p_arg
temp_type = args.temp_type

# build tables
t_table = []
s_table = []
for file in files:
	fid = open(file)
	filetype = file.split('.')[-1]
	p = wod.WodProfile(fid)
	while True:
		if y != p.year() or m != p.month():
			#continue
			break

		pindex = p.var_index(25)

		# extract and QC filter in situ measurements
		temp,psal,pres = helpers.filterQCandPressure(p.t(), p.s(), p.p(), p.t_level_qc(originator=False), p.s_level_qc(originator=False), p.var_level_qc(pindex), [0], 10000000)
		if len(pres) == 0:
			print(p.uid(), 'no data passing QC')
			if p.is_last_profile_in_file(fid):
				break
			else:
				p = wod.WodProfile(fid)
			continue

		# make sure there's meaningful data in range:
		## single level interpolation: a level with both temp and salinity within 15 dbar of p_interp
		if p_interp:
			p_min_i, p_max_i = helpers.pad_bracket(pres, p_interp, p_interp, 15, 0)
			t_in_radius = temp[p_min_i+1:p_max_i]
			s_in_radius = psal[p_min_i+1:p_max_i]
			if not helpers.has_common_non_nan_value(t_in_radius, s_in_radius):
				print(p.uid(), 'no data in range')
				if p.is_last_profile_in_file(fid):
					break
				else:
					p = wod.WodProfile(fid)
				continue
		## range integral: entire integral range is inside the pressure range found in the profile
		elif p_range:
			if p_range[0] < pres[0] or p_range[1] > pres[-1]:
				if p.is_last_profile_in_file(fid):
					break
				else:
					p = wod.WodProfile(fid)
				continue

		# narrow down levels considered to things near the region of interest
		near = args.pressure_buffer # dbar on either side of the level or integration region
		places = args.pressure_index_buffer # make sure we're keeping at least 5 levels above and below the ROI
		if p_interp:
			p_bracket = helpers.pad_bracket(pres, p_interp, p_interp, near, places)
		elif p_range:
			p_bracket = helpers.pad_bracket(pres, p_range[0], p_range[1], near, places)
		p_region = pres[p_bracket[0]:p_bracket[1]+1]
		t_region = temp[p_bracket[0]:p_bracket[1]+1]
		s_region = psal[p_bracket[0]:p_bracket[1]+1]

		# degenerate pressure levels ruin the interpolation, bail out if found
		if helpers.has_repeated_elements(p_region):
			print(p.uid(), 'degenerate levels')
			if p.is_last_profile_in_file(fid):
				break
			else:
				p = wod.WodProfile(fid)
			continue

		# compute absolute salinity
		abs_sal = [gsw.conversions.SA_from_SP(s_region[i], p_region[i], p.longitude(), p.latitude()) for i in range(len(p_region))]

		# compute potential or conservative temperature; t_star will be whichever one we're interested in
		if temp_type == 'potential':
			t_potential = gsw.conversions.pt0_from_t(abs_sal, t_region, p_region)
			t_star = [t + 273.15 for t in t_potential]
		elif temp_type == 'conservative':
			t_conservative = gsw.conversions.CT_from_t(abs_sal, t_region, p_region)
			t_star = [t + 273.15 for t in t_conservative]

		# interpolate to specific level:
		if p_interp:
			if not numpy.isnan(t_star).all():
				try:
					t_interp = scipy.interpolate.pchip_interpolate(p_region, t_star, p_interp)
					t_table.append([
						helpers.mljul(p.year(),p.month(),p.day(),p.time()),
						helpers.remap_longitude(p.longitude()), 
						p.latitude(), 
						p.month(),
						[p_interp],
						[t_interp],
						p.year(),
						#filetype,
						0,
						0
					])	
				except:
					print(p.uid())
					print('pressure', p_region)
					print('temperature', t_region)
			if not numpy.isnan(abs_sal).all():
				try:
					s_interp = scipy.interpolate.pchip_interpolate(p_region, abs_sal, p_interp)
					s_table.append([
						helpers.mljul(p.year(),p.month(),p.day(),p.time()),
						helpers.remap_longitude(p.longitude()), 
						p.latitude(), 
						p.month(),
						[p_interp],
						[s_interp],
						p.year(),
						#filetype,
						0,
						0
					])
				except:
					print(p.uid())
					print('pressure', p_region)
					print('salinity', abs_sal)
		# or, integrate across ROI
		elif p_range:
			if not numpy.isnan(t_star).all():
				try:
					t_integrate = helpers.interpolate_and_integrate(p_region, t_star, p_range[0], p_range[1])
					t_table.append([
						helpers.mljul(p.year(),p.month(),p.day(),p.time()),
						helpers.remap_longitude(p.longitude()), 
						p.latitude(), 
						p.month(),
						p_range,
						[t_integrate],
						p.year(),
						#filetype,
						0,
						0
					])	
				except:
					print(p.uid())
					print('pressure', p_region)
					print('temperature', t_region)
			if not numpy.isnan(abs_sal).all():
				try:
					s_integrate = helpers.interpolate_and_integrate(p_region, abs_sal, p_range[0], p_range[1])
					s_table.append([
						helpers.mljul(p.year(),p.month(),p.day(),p.time()),
						helpers.remap_longitude(p.longitude()), 
						p.latitude(), 
						p.month(),
						p_range,
						[s_integrate],
						p.year(),
						#filetype,
						0,
						0
					])
				except:
					print(p.uid())
					print('pressure', p_region)
					print('salinity', abs_sal)

		if p.is_last_profile_in_file(fid):
			break
		else:
			p = wod.WodProfile(fid)

# remove profiles that are exactly colocated and less than 15 minutes apart
t_table = helpers.sort_and_remove_neighbors(t_table, 1,2,0)
s_table = helpers.sort_and_remove_neighbors(s_table, 1,2,0)

# choose names for whatever it was we just calculated
if p_interp and temp_type == 'potential':
	tname = 'potentialTemperature'
elif p_interp and temp_type == 'conservative':
	tname = 'conservativeTemperature'
elif p_range and temp_type == 'potential':
	tname = 'potentialTemperatureIntegral'
elif p_range and temp_type == 'conservative':
	tname = 'conservativeTemperatureIntegral'
if p_interp:
	sname = 'absoluteSalinity'
	pname = 'interpolatedPressure'
elif p_range:
	sname = 'absoluteSalinityIntegral'
	pname = 'pressureRange'

t_df = pandas.DataFrame(t_table, columns = [
		'profJulDayAggr',  
		'profLongAggr', 
		'profLatAggr', 
		'profMonthAggr',
		pname, 
		tname, 
		'profYearAggr', 
		#'WODtype', 
		'profCycleNumberAggr', 
		'profFloatIDAggr'
	]) 

scipy.io.savemat(f'{args.out_dir}/{tname}_{y}_{m}_{"_".join(map(str, args.pressure))}.mat', t_df.to_dict("list"))

s_df = pandas.DataFrame(s_table, columns = [
		'profJulDayAggr',  
		'profLongAggr', 
		'profLatAggr', 
		'profMonthAggr',
		pname, 
		sname, 
		'profYearAggr', 
		#'WODtype', 
		'profCycleNumberAggr', 
		'profFloatIDAggr'
	]) 

scipy.io.savemat(f'{args.out_dir}/{sname}_{y}_{m}_{"_".join(map(str, args.pressure))}.mat', s_df.to_dict("list"))

