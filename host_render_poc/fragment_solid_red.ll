source_filename = "fragment_solid_red.metal"
target datalayout = "e-p:64:64:64"
target triple = "air64-apple-macosx14.0.0"

define <4 x float> @frag(<4 x float> %position) local_unnamed_addr #0 {
  %r = insertelement <4 x float> undef, float 1.000000e+00, i64 0
  %rg = insertelement <4 x float> %r, float 0.000000e+00, i64 1
  %rgb = insertelement <4 x float> %rg, float 0.000000e+00, i64 2
  %rgba = insertelement <4 x float> %rgb, float 1.000000e+00, i64 3
  ret <4 x float> %rgba
}

attributes #0 = { nounwind }

!air.fragment = !{!0}
!0 = !{ptr @frag, !1, !2}
!1 = !{!3}
!2 = !{!4}
!3 = !{i32 0, !"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!4 = !{i32 0, !"air.position", !"air.center", !"air.arg_type_name", !"float4"}
