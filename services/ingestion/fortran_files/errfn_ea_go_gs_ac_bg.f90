PROGRAM errfn_ea_go_gs_ac_bg
!
! Calculate the cost function for the ea_go_gs_ac_bg problem
!
! The binary generated by this code is designed to post-process the 
! netcdf output from the ea_go_gs_ac_bg model and calculate a RMS
! error function. This binary should reside in the same location as
! the genie.exe binary and will read the goin files in that directory
! to find the details of the data directories, topography files and
! observational data files. The error functions are calculated by
! providing the model data and observational data to the appropriate
! error function codes. RMS error values are output in the file
! errfn_ea_go_gs_ac_bg.err.
!
! Andrew Price - 14/02/07
!
IMPLICIT NONE
#ifndef GOLDSTEINNLONS
#define GOLDSTEINNLONS 36
#endif
#ifndef GOLDSTEINNLATS
#define GOLDSTEINNLATS 36
#endif
#ifndef GOLDSTEINNLEVS
#define GOLDSTEINNLEVS 8
#endif
INCLUDE '../../netcdf.inc'

! Grids
INTEGER, PARAMETER :: imax=GOLDSTEINNLONS, jmax=GOLDSTEINNLATS, kmax=GOLDSTEINNLEVS

! File inquiry
CHARACTER(LEN=256) :: filename
LOGICAL            :: exists

! NetCDF variables
INTEGER            :: ncid, status

! Data directories
CHARACTER(LEN=100) :: indir_name, outdir_name

! Target and model data fields
REAL, DIMENSION(imax,jmax,kmax,1)    :: PO4_target, ALK_target
REAL, DIMENSION(imax,jmax,kmax,1)    :: PO4_model, ALK_model

! Target and model ocean masks
LOGICAL, DIMENSION(imax,jmax,kmax,1) :: PO4_mask, ALK_mask
INTEGER                              :: PO4_pts, ALK_pts

! Target data variance
REAL                                 :: PO4_target_var, ALK_target_var

! Weighting factors for the ocean grid cells
REAL, DIMENSION(imax,jmax,kmax,1)    :: weights

! RMS Errors
REAL, DIMENSION(2) :: rmserror

INTEGER :: i,j,k

!======================================================================
! Initialise
!
! Assume: this binary is executing in same location as genie.exe and
!         therefore goin_* and data_* files are available which will
!         tell us where the data is
!======================================================================

! Process the goin_BIOGEM file
INQUIRE(FILE='goin_BIOGEM', EXIST=exists)
IF (.NOT. exists) THEN
    PRINT*,'Cannot find ./goin_BIOGEM'
    STOP
END IF

! Read the BIOGEM GOIN file
open(unit=55,file='goin_BIOGEM',status='old')

! Input directory name
read(55,*) indir_name
indir_name = trim(indir_name)//'/'

! Output directory name
read(55,*) outdir_name
outdir_name = trim(outdir_name)//'/'

! Close the goin file
close(unit=55)

!======================================================================
! Load the final state model data (ann. av.?)
!======================================================================

! Model output data file
filename=trim(outdir_name)//'fields_biogem_3d.nc'
print*,'Opening file ',filename

! Check that the model output data file exists
INQUIRE(FILE=filename, EXIST=exists)
IF (.NOT. exists) THEN
    PRINT*,'Cannot find ./'//filename
    STOP
END IF

! Open the file
status=nf_open(trim(filename), 0, ncid)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Load the PO4 data
call get4d_data_nc(ncid, 'ocn_PO4_Snorm', imax, jmax, kmax, 1, PO4_target, status)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Load the ALK data
call get4d_data_nc(ncid, 'ocn_ALK_Snorm', imax, jmax, kmax, 1, ALK_target, status)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Close the data file
status=nf_close(ncid)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

!======================================================================
! Load the observational target data
!======================================================================

! Target input data file
filename=trim(indir_name)//'fields_biogem_3d.nc'
print*,'Opening file ',filename

! Check that the obs target input data file exists
INQUIRE(FILE=filename, EXIST=exists)
IF (.NOT. exists) THEN
    PRINT*,'Cannot find ./'//filename
    STOP
END IF

! Open the file
status=nf_open(trim(filename), 0, ncid)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Load the PO4 model field
call get4d_data_nc(ncid, 'ocn_PO4_Snorm', imax, jmax, kmax, 1, PO4_model, status)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Load the ALK model field
call get4d_data_nc(ncid, 'ocn_ALK_Snorm', imax, jmax, kmax, 1, ALK_model, status)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

! Close the data file
status=nf_close(ncid)
IF (status .NE. NF_NOERR) CALL CHECK_ERR(status)

!======================================================================
! Calculate the individual RMS errors for each field
!======================================================================

! Find the ocean grid cell mask in the model data
! Biogem seems to set these values very high, find points < 1e20
do k=1,kmax
    do j=1,jmax
        do i=1,imax
            if ((PO4_model(i,j,k,1)<1e20).neqv.(PO4_target(i,j,k,1)<1e20)) then
                PRINT*,'PO4: Model and Target data sets do not have the same number of cells in the ocean'
                STOP
            end if
        end do
    end do
end do

do k=1,kmax
    do j=1,jmax
        do i=1,imax
            if ((ALK_model(i,j,k,1)<1e20).neqv.(ALK_target(i,j,k,1)<1e20)) then
                PRINT*,'ALK: Model and Target data sets do not have the same number of cells in the ocean'
                STOP
            end if
        end do
    end do
end do

PO4_mask = (PO4_model<1e20)
PO4_pts  = count(PO4_mask)
ALK_mask = (ALK_model<1e20)
ALK_pts  = count(ALK_mask)

! Calculate the variances in the target data sets
weights=1.0  ! Can weight by real world cell area here if desired
PO4_target_var = sum(weights*PO4_target**2, MASK=PO4_mask)/PO4_pts - (sum(weights*PO4_target, MASK=PO4_mask)/PO4_pts)**2
ALK_target_var = sum(weights*ALK_target**2, MASK=ALK_mask)/ALK_pts - (sum(weights*ALK_target, MASK=ALK_mask)/ALK_pts)**2

! Calculate the RMS error in the two objectives
rmserror(1) = sqrt( sum( (weights*(PO4_model - PO4_target))**2, MASK=PO4_mask) / (PO4_pts*PO4_target_var) )
rmserror(2) = sqrt( sum( (weights*(ALK_model - ALK_target))**2, MASK=ALK_mask) / (ALK_pts*ALK_target_var) )

!======================================================================
! Calculate a composite cost function
!======================================================================

! Echo the errors and number of points in the calculation
print *,'BIOGEM PO4 RMS Error: ',rmserror(1), PO4_pts
print *,'BIOGEM ALK RMS Error: ',rmserror(2), ALK_pts

! Output the errors to file
open(unit=433, file='errfn_ea_go_gs_ac_bg.err')
write(433,435) rmserror(1), rmserror(2)
write(433,436) PO4_pts, ALK_pts
435 format(2f20.16)
436 format(2i20)
close(433)

END PROGRAM errfn_ea_go_gs_ac_bg